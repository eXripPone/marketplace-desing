package booking

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"time"

	pb "flightbooking/generated/flightv1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Service struct {
	db      *sql.DB
	flights pb.FlightServiceClient
}

func New(db *sql.DB, flights pb.FlightServiceClient) *Service {
	return &Service{db: db, flights: flights}
}

type Booking struct {
	ID              string     `json:"id"`
	UserID          string     `json:"user_id"`
	FlightID        int64      `json:"flight_id"`
	PassengerName   string     `json:"passenger_name"`
	PassengerEmail  string     `json:"passenger_email"`
	SeatCount       int32      `json:"seat_count"`
	TotalPriceCents int64      `json:"total_price_cents"`
	Status          string     `json:"status"`
	CreatedAt       time.Time  `json:"created_at"`
	CancelledAt     *time.Time `json:"cancelled_at,omitempty"`
}

type createRequest struct {
	UserID         string `json:"user_id"`
	FlightID       int64  `json:"flight_id"`
	PassengerName  string `json:"passenger_name"`
	PassengerEmail string `json:"passenger_email"`
	SeatCount      int32  `json:"seat_count"`
}

const bookingColumns = "id::text,user_id,flight_id,passenger_name,passenger_email,seat_count,total_price_cents,status,created_at,cancelled_at"
const bookingSelect = "SELECT " + bookingColumns + " FROM bookings"

func scanBooking(row interface{ Scan(...any) error }) (Booking, error) {
	var b Booking
	err := row.Scan(&b.ID, &b.UserID, &b.FlightID, &b.PassengerName, &b.PassengerEmail,
		&b.SeatCount, &b.TotalPriceCents, &b.Status, &b.CreatedAt, &b.CancelledAt)
	return b, err
}

func dbError(err error) error {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return status.FromContextError(err).Err()
	}
	log.Printf("booking database: %v", err)
	return status.Error(codes.Internal, "booking database error")
}

func bookingRead(b Booking, err error) (Booking, error) {
	if errors.Is(err, sql.ErrNoRows) {
		return b, status.Error(codes.NotFound, "booking not found")
	}
	if err != nil {
		return b, dbError(err)
	}
	return b, nil
}

func (s *Service) create(ctx context.Context, req createRequest) (Booking, error) {
	f, err := s.flights.GetFlight(ctx, &pb.GetFlightRequest{FlightId: req.FlightID})
	if err != nil {
		return Booking{}, err
	}
	id := uuid.NewString()
	// This is a recovery record, not a booking. A failed reservation leaves no booking row.
	_, err = s.db.ExecContext(ctx, "INSERT INTO flight_operations(booking_id,action) VALUES($1,'cleanup')", id)
	if err != nil {
		return Booking{}, dbError(err)
	}

	_, err = s.flights.ReserveSeats(ctx, &pb.ReserveSeatsRequest{FlightId: req.FlightID, SeatCount: req.SeatCount, BookingId: id})
	if err != nil {
		// For a definite rejection there is nothing to compensate. A lost reply is ambiguous.
		switch status.Code(err) {
		case codes.NotFound, codes.InvalidArgument, codes.ResourceExhausted, codes.FailedPrecondition, codes.Unauthenticated:
			cleanupCtx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			if _, cleanupErr := s.db.ExecContext(cleanupCtx, "DELETE FROM flight_operations WHERE booking_id=$1", id); cleanupErr != nil {
				log.Printf("cleanup marker %s: %v", id, cleanupErr)
			}
		}
		return Booking{}, err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Booking{}, dbError(err)
	}
	defer tx.Rollback()
	b, err := scanBooking(tx.QueryRowContext(ctx, `INSERT INTO bookings
 (id,user_id,flight_id,passenger_name,passenger_email,seat_count,total_price_cents,status)
 VALUES($1,$2,$3,$4,$5,$6,$7,'CONFIRMED') RETURNING `+bookingColumns,
		id, req.UserID, req.FlightID, req.PassengerName, req.PassengerEmail, req.SeatCount, int64(req.SeatCount)*f.PriceCents))
	if err != nil {
		return Booking{}, dbError(err)
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM flight_operations WHERE booking_id=$1", id); err != nil {
		return Booking{}, dbError(err)
	}
	if err := tx.Commit(); err != nil {
		return Booking{}, dbError(err)
	}
	return b, nil
}

func (s *Service) cancel(ctx context.Context, id string) (Booking, error) {
	b, err := bookingRead(scanBooking(s.db.QueryRowContext(ctx, bookingSelect+" WHERE id=$1", id)))
	if err != nil {
		return b, err
	}
	if b.Status != "CONFIRMED" {
		return b, status.Error(codes.FailedPrecondition, "booking is already cancelled")
	}
	// Durable intent allows retry after a successful release followed by a local DB failure.
	_, err = s.db.ExecContext(ctx, `INSERT INTO flight_operations(booking_id,action)
 SELECT id,'cancel' FROM bookings WHERE id=$1 AND status='CONFIRMED'
 ON CONFLICT (booking_id) DO NOTHING`, id)
	if err != nil {
		return b, dbError(err)
	}
	return s.finishCancel(ctx, id)
}

func (s *Service) finishCancel(ctx context.Context, id string) (Booking, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Booking{}, dbError(err)
	}
	defer tx.Rollback()
	b, err := bookingRead(scanBooking(tx.QueryRowContext(ctx, bookingSelect+" WHERE id=$1 FOR UPDATE", id)))
	if err != nil {
		return b, err
	}
	if b.Status == "CONFIRMED" {
		_, err = s.flights.ReleaseReservation(ctx, &pb.ReleaseReservationRequest{BookingId: id})
		if err != nil {
			return b, err
		}
		b, err = scanBooking(tx.QueryRowContext(ctx, `UPDATE bookings SET status='CANCELLED',cancelled_at=now()
   WHERE id=$1 RETURNING `+bookingColumns, id))
		if err != nil {
			return b, dbError(err)
		}
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM flight_operations WHERE booking_id=$1", id); err != nil {
		return b, dbError(err)
	}
	if err := tx.Commit(); err != nil {
		return b, dbError(err)
	}
	return b, nil
}

// Recovery runs in the Booking process; it does not need a separate queue or worker container.
func (s *Service) Recover(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.recoverOnce(ctx)
		}
	}
}

func (s *Service) recoverOnce(ctx context.Context) {
	// HTTP handlers have a 12 second budget. Wait 30 seconds before compensating an interrupted operation.
	rows, err := s.db.QueryContext(ctx, `SELECT booking_id::text,action FROM flight_operations
  WHERE created_at < now()-interval '30 seconds' ORDER BY created_at LIMIT 20`)
	if err != nil {
		log.Printf("recovery query: %v", err)
		return
	}
	type operation struct{ id, action string }
	var pending []operation
	for rows.Next() {
		var op operation
		if err = rows.Scan(&op.id, &op.action); err != nil {
			break
		}
		pending = append(pending, op)
	}
	rows.Close()
	for _, op := range pending {
		attempt, cancel := context.WithTimeout(ctx, 4*time.Second)
		if op.action == "cancel" {
			_, err = s.finishCancel(attempt, op.id)
		} else {
			_, err = s.flights.ReleaseReservation(attempt, &pb.ReleaseReservationRequest{BookingId: op.id})
			if err == nil || status.Code(err) == codes.NotFound {
				_, err = s.db.ExecContext(attempt, "DELETE FROM flight_operations WHERE booking_id=$1 AND action='cleanup'", op.id)
			}
		}
		cancel()
		if err != nil {
			log.Printf("recovery %s %s: %v", op.action, op.id, err)
		} else {
			log.Printf("recovered %s %s", op.action, op.id)
		}
	}
}
