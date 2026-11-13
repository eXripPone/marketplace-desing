package flight

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"errors"
	"log"
	"regexp"
	"time"

	pb "flightbooking/generated/flightv1"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Service struct {
	pb.UnimplementedFlightServiceServer
	db    *sql.DB
	cache *cache
}

func New(db *sql.DB, redisClient *redis.Client) *Service {
	return &Service{db: db, cache: newCache(redisClient)}
}

func Auth(key string) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		values := metadata.ValueFromIncomingContext(ctx, "x-api-key")
		if len(values) != 1 || subtle.ConstantTimeCompare([]byte(values[0]), []byte(key)) != 1 {
			return nil, status.Error(codes.Unauthenticated, "invalid service credentials")
		}
		return handler(ctx, req)
	}
}

const flightSelect = `SELECT f.id, f.flight_number, a.code, a.name, f.origin, f.destination,
 f.departure_at, f.arrival_at, f.total_seats, f.available_seats, f.price_cents, f.status
 FROM flights f JOIN airlines a ON a.code = f.airline_code`

type scanner interface{ Scan(...any) error }
type querier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func scanFlight(row scanner) (*pb.Flight, error) {
	f := new(pb.Flight)
	var departure, arrival time.Time
	var state string
	err := row.Scan(&f.Id, &f.FlightNumber, &f.AirlineCode, &f.AirlineName, &f.Origin, &f.Destination,
		&departure, &arrival, &f.TotalSeats, &f.AvailableSeats, &f.PriceCents, &state)
	if err != nil {
		return nil, err
	}
	f.DepartureAt, f.ArrivalAt = timestamppb.New(departure), timestamppb.New(arrival)
	f.Status = pb.FlightStatus(pb.FlightStatus_value[state])
	return f, nil
}

func selectFlight(ctx context.Context, db querier, id int64, lock bool) (*pb.Flight, error) {
	query := flightSelect + " WHERE f.id = $1"
	if lock {
		query += " FOR UPDATE OF f"
	}
	f, err := scanFlight(db.QueryRowContext(ctx, query, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "flight not found")
	}
	if err != nil {
		return nil, dbError(err)
	}
	return f, nil
}

func dbError(err error) error {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return status.FromContextError(err).Err()
	}
	log.Printf("flight database: %v", err)
	return status.Error(codes.Internal, "flight database error")
}

var iata = regexp.MustCompile("^[A-Z]{3}$")

func (s *Service) SearchFlights(ctx context.Context, req *pb.SearchFlightsRequest) (*pb.SearchFlightsResponse, error) {
	if !iata.MatchString(req.Origin) || !iata.MatchString(req.Destination) || req.Origin == req.Destination {
		return nil, status.Error(codes.InvalidArgument, "origin and destination must be distinct IATA codes")
	}
	day := "all"
	args := []any{req.Origin, req.Destination}
	query := flightSelect + " WHERE f.origin = $1 AND f.destination = $2 AND f.status = 'SCHEDULED'"
	if req.DepartureDate != nil {
		if err := req.DepartureDate.CheckValid(); err != nil {
			return nil, status.Error(codes.InvalidArgument, "invalid departure date")
		}
		t := req.DepartureDate.AsTime().UTC()
		start := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
		day = start.Format("2006-01-02")
		query += " AND f.departure_at >= $3 AND f.departure_at < $4"
		args = append(args, start, start.AddDate(0, 0, 1))
	}
	query += " ORDER BY f.departure_at, f.id"
	result := new(pb.SearchFlightsResponse)
	err := s.cache.read(ctx, searchKey(req.Origin, req.Destination, day), result, func() error {
		rows, err := s.db.QueryContext(ctx, query, args...)
		if err != nil {
			return dbError(err)
		}
		defer rows.Close()
		result.Flights = nil
		for rows.Next() {
			f, err := scanFlight(rows)
			if err != nil {
				return dbError(err)
			}
			result.Flights = append(result.Flights, f)
		}
		if err := rows.Err(); err != nil {
			return dbError(err)
		}
		return nil
	})
	return result, err
}

func (s *Service) GetFlight(ctx context.Context, req *pb.GetFlightRequest) (*pb.Flight, error) {
	if req.FlightId < 1 {
		return nil, status.Error(codes.InvalidArgument, "invalid flight ID")
	}
	result := new(pb.Flight)
	err := s.cache.read(ctx, flightKey(req.FlightId), result, func() error {
		f, err := selectFlight(ctx, s.db, req.FlightId, false)
		if err != nil {
			return err
		}
		proto.Merge(result, f)
		return nil
	})
	return result, err
}

const reservationSelect = `SELECT booking_id::text, flight_id, seat_count, status, created_at
 FROM seat_reservations WHERE booking_id = $1`

func scanReservation(row scanner) (*pb.SeatReservation, error) {
	res := new(pb.SeatReservation)
	var state string
	var created time.Time
	err := row.Scan(&res.BookingId, &res.FlightId, &res.SeatCount, &state, &created)
	if err != nil {
		return nil, err
	}
	res.Status = pb.ReservationStatus(pb.ReservationStatus_value[state])
	res.CreatedAt = timestamppb.New(created)
	return res, nil
}

// Locks a booking ID even before its first reservation row exists.
func lockBooking(ctx context.Context, tx *sql.Tx, id string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", id)
	return err
}

func (s *Service) ReserveSeats(ctx context.Context, req *pb.ReserveSeatsRequest) (*pb.SeatReservation, error) {
	id, err := uuid.Parse(req.BookingId)
	if err != nil || id == uuid.Nil || req.FlightId < 1 || req.SeatCount < 1 || req.SeatCount > 1000 {
		return nil, status.Error(codes.InvalidArgument, "valid flight ID, booking UUID and seat count (1-1000) required")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback()
	if err := lockBooking(ctx, tx, id.String()); err != nil {
		return nil, dbError(err)
	}
	existing, err := scanReservation(tx.QueryRowContext(ctx, reservationSelect, id.String()))
	if err == nil {
		if existing.FlightId != req.FlightId || existing.SeatCount != req.SeatCount {
			return nil, status.Error(codes.AlreadyExists, "booking ID already has a different reservation")
		}
		if existing.Status != pb.ReservationStatus_ACTIVE {
			return nil, status.Error(codes.FailedPrecondition, "reservation is no longer active")
		}
		return existing, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, dbError(err)
	}

	f, err := selectFlight(ctx, tx, req.FlightId, true)
	if err != nil {
		return nil, err
	}
	if f.Status != pb.FlightStatus_SCHEDULED {
		return nil, status.Error(codes.FailedPrecondition, "flight is not scheduled")
	}
	if f.AvailableSeats < req.SeatCount {
		return nil, status.Error(codes.ResourceExhausted, "not enough available seats")
	}
	if _, err := tx.ExecContext(ctx, "UPDATE flights SET available_seats=available_seats-$2 WHERE id=$1", f.Id, req.SeatCount); err != nil {
		return nil, dbError(err)
	}
	reservation, err := scanReservation(tx.QueryRowContext(ctx, `INSERT INTO seat_reservations(booking_id,flight_id,seat_count,status)
 VALUES($1,$2,$3,'ACTIVE') RETURNING booking_id::text,flight_id,seat_count,status,created_at`, id.String(), f.Id, req.SeatCount))
	if err != nil {
		return nil, dbError(err)
	}
	if err := tx.Commit(); err != nil {
		return nil, dbError(err)
	}
	s.cache.invalidate(f)
	return reservation, nil
}

func (s *Service) ReleaseReservation(ctx context.Context, req *pb.ReleaseReservationRequest) (*pb.SeatReservation, error) {
	id, err := uuid.Parse(req.BookingId)
	if err != nil || id == uuid.Nil {
		return nil, status.Error(codes.InvalidArgument, "valid booking UUID required")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback()
	if err := lockBooking(ctx, tx, id.String()); err != nil {
		return nil, dbError(err)
	}
	res, err := scanReservation(tx.QueryRowContext(ctx, reservationSelect+" FOR UPDATE", id.String()))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "reservation not found")
	}
	if err != nil {
		return nil, dbError(err)
	}
	if res.Status != pb.ReservationStatus_ACTIVE {
		return res, nil
	}

	f, err := selectFlight(ctx, tx, res.FlightId, true)
	if err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE flights SET available_seats=available_seats+$2 WHERE id=$1", f.Id, res.SeatCount); err != nil {
		return nil, dbError(err)
	}
	if _, err := tx.ExecContext(ctx, "UPDATE seat_reservations SET status='RELEASED' WHERE booking_id=$1", id.String()); err != nil {
		return nil, dbError(err)
	}
	if err := tx.Commit(); err != nil {
		return nil, dbError(err)
	}
	res.Status = pb.ReservationStatus_RELEASED
	s.cache.invalidate(f)
	return res, nil
}

func (s *Service) UpdateFlight(ctx context.Context, req *pb.UpdateFlightRequest) (*pb.Flight, error) {
	if req.FlightId < 1 || (req.PriceCents == nil && req.Status == nil) {
		return nil, status.Error(codes.InvalidArgument, "flight ID and at least one change required")
	}
	if req.PriceCents != nil && (*req.PriceCents < 1 || *req.PriceCents > 1000000000000) {
		return nil, status.Error(codes.InvalidArgument, "price_cents must be between 1 and 1000000000000")
	}
	var state *string
	if req.Status != nil {
		value, ok := pb.FlightStatus_name[int32(*req.Status)]
		if !ok || *req.Status == pb.FlightStatus_FLIGHT_STATUS_UNSPECIFIED {
			return nil, status.Error(codes.InvalidArgument, "invalid flight status")
		}
		state = &value
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, dbError(err)
	}
	defer tx.Rollback()
	if _, err := selectFlight(ctx, tx, req.FlightId, true); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE flights SET price_cents=COALESCE($2,price_cents), status=COALESCE($3,status) WHERE id=$1",
		req.FlightId, req.PriceCents, state); err != nil {
		return nil, dbError(err)
	}
	f, err := selectFlight(ctx, tx, req.FlightId, false)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, dbError(err)
	}
	s.cache.invalidate(f)
	return f, nil
}
