package booking

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/mail"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	pb "flightbooking/generated/flightv1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		if err := s.db.PingContext(r.Context()); err != nil {
			writeError(w, status.Error(codes.Unavailable, "booking database unavailable"))
			return
		}
		respond(w, 200, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /flights", s.searchFlights)
	mux.HandleFunc("GET /flights/{id}", s.getFlight)
	mux.HandleFunc("POST /bookings", s.createBooking)
	mux.HandleFunc("GET /bookings", s.listBookings)
	mux.HandleFunc("GET /bookings/{id}", s.getBooking)
	mux.HandleFunc("POST /bookings/{id}/cancel", s.cancelBooking)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 12*time.Second)
		defer cancel()
		log.Printf("%s %s", r.Method, r.URL.Path)
		mux.ServeHTTP(w, r.WithContext(ctx))
	})
}

type flightResponse struct {
	ID             int64     `json:"id"`
	FlightNumber   string    `json:"flight_number"`
	AirlineCode    string    `json:"airline_code"`
	AirlineName    string    `json:"airline_name"`
	Origin         string    `json:"origin"`
	Destination    string    `json:"destination"`
	DepartureAt    time.Time `json:"departure_at"`
	ArrivalAt      time.Time `json:"arrival_at"`
	TotalSeats     int32     `json:"total_seats"`
	AvailableSeats int32     `json:"available_seats"`
	PriceCents     int64     `json:"price_cents"`
	Status         string    `json:"status"`
}

func flightJSON(f *pb.Flight) flightResponse {
	return flightResponse{
		ID: f.Id, FlightNumber: f.FlightNumber, AirlineCode: f.AirlineCode, AirlineName: f.AirlineName,
		Origin: f.Origin, Destination: f.Destination, DepartureAt: f.DepartureAt.AsTime(), ArrivalAt: f.ArrivalAt.AsTime(),
		TotalSeats: f.TotalSeats, AvailableSeats: f.AvailableSeats, PriceCents: f.PriceCents, Status: f.Status.String(),
	}
}

func (s *Service) searchFlights(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	req := &pb.SearchFlightsRequest{Origin: strings.ToUpper(strings.TrimSpace(q.Get("origin"))),
		Destination: strings.ToUpper(strings.TrimSpace(q.Get("destination")))}
	if date := q.Get("date"); date != "" {
		parsed, err := time.Parse("2006-01-02", date)
		if err != nil {
			writeError(w, invalid("date must have format YYYY-MM-DD"))
			return
		}
		req.DepartureDate = timestamppb.New(parsed)
	}
	result, err := s.flights.SearchFlights(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	flights := make([]flightResponse, 0, len(result.Flights))
	for _, f := range result.Flights {
		flights = append(flights, flightJSON(f))
	}
	respond(w, 200, flights)
}

func (s *Service) getFlight(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id < 1 {
		writeError(w, invalid("invalid flight ID"))
		return
	}
	f, err := s.flights.GetFlight(r.Context(), &pb.GetFlightRequest{FlightId: id})
	if err != nil {
		writeError(w, err)
		return
	}
	respond(w, 200, flightJSON(f))
}

func (s *Service) createBooking(w http.ResponseWriter, r *http.Request) {
	var req createRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16*1024))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeError(w, invalid("invalid booking JSON"))
		return
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		writeError(w, invalid("expected one JSON object"))
		return
	}

	req.UserID = strings.TrimSpace(req.UserID)
	req.PassengerName = strings.TrimSpace(req.PassengerName)
	req.PassengerEmail = strings.TrimSpace(req.PassengerEmail)
	addr, emailErr := mail.ParseAddress(req.PassengerEmail)
	if !validText(req.UserID, 100) || !validText(req.PassengerName, 200) ||
		!validText(req.PassengerEmail, 254) || emailErr != nil || addr.Address != req.PassengerEmail ||
		req.FlightID < 1 || req.SeatCount < 1 || req.SeatCount > 1000 {
		writeError(w, invalid("valid user_id, flight_id, passenger_name, passenger_email and seat_count (1-1000) required"))
		return
	}
	b, err := s.create(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	w.Header().Set("Location", "/bookings/"+b.ID)
	respond(w, 201, b)
}

func (s *Service) getBooking(w http.ResponseWriter, r *http.Request) {
	id, err := parseBookingID(r)
	if err != nil {
		writeError(w, err)
		return
	}
	b, err := bookingRead(scanBooking(s.db.QueryRowContext(r.Context(), bookingSelect+" WHERE id=$1", id)))
	if err != nil {
		writeError(w, err)
		return
	}
	respond(w, 200, b)
}

func (s *Service) cancelBooking(w http.ResponseWriter, r *http.Request) {
	id, err := parseBookingID(r)
	if err != nil {
		writeError(w, err)
		return
	}
	b, err := s.cancel(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	respond(w, 200, b)
}

func (s *Service) listBookings(w http.ResponseWriter, r *http.Request) {
	user := strings.TrimSpace(r.URL.Query().Get("user_id"))
	if !validText(user, 100) {
		writeError(w, invalid("user_id is required"))
		return
	}
	rows, err := s.db.QueryContext(r.Context(), bookingSelect+" WHERE user_id=$1 ORDER BY created_at DESC,id", user)
	if err != nil {
		writeError(w, dbError(err))
		return
	}
	defer rows.Close()
	result := make([]Booking, 0)
	for rows.Next() {
		b, err := scanBooking(rows)
		if err != nil {
			writeError(w, dbError(err))
			return
		}
		result = append(result, b)
	}
	if err := rows.Err(); err != nil {
		writeError(w, dbError(err))
		return
	}
	respond(w, 200, result)
}

func validText(s string, max int) bool {
	return s != "" && !strings.ContainsRune(s, 0) && utf8.RuneCountInString(s) <= max
}

func parseBookingID(r *http.Request) (string, error) {
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil || id == uuid.Nil {
		return "", invalid("invalid booking UUID")
	}
	return id.String(), nil
}

func invalid(message string) error { return status.Error(codes.InvalidArgument, message) }

func respond(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, err error) {
	s := status.Convert(err)
	httpCode := 500
	switch s.Code() {
	case codes.InvalidArgument:
		httpCode = 400
	case codes.NotFound:
		httpCode = 404
	case codes.ResourceExhausted, codes.AlreadyExists, codes.FailedPrecondition:
		httpCode = 409
	case codes.Unavailable, codes.DeadlineExceeded, codes.Canceled:
		httpCode = 503
	case codes.Unauthenticated:
		httpCode = 502 // Service credentials, not client credentials.
	}
	respond(w, httpCode, map[string]string{"code": s.Code().String(), "message": s.Message()})
}
