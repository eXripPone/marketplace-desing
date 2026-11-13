package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	pb "flightbooking/generated/flightv1"
	"flightbooking/internal/booking"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func flightClient(options ...grpc.DialOption) (*grpc.ClientConn, error) {
	options = append(options, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	return grpc.NewClient("passthrough:///"+env("FLIGHT_ADDR", "127.0.0.1:50051"), options...)
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("usage: check ready|http|verify")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	var err error
	switch os.Args[1] {
	case "http":
		err = request(ctx, "GET", "/health", nil, 200, nil)
	case "ready":
		conn, e := flightClient()
		if e != nil {
			log.Fatal(e)
		}
		defer conn.Close()
		ready, stop := context.WithTimeout(ctx, time.Second)
		defer stop()
		ready = metadata.AppendToOutgoingContext(ready, "x-api-key", os.Getenv("FLIGHT_API_KEY"))
		_, err = pb.NewFlightServiceClient(conn).SearchFlights(ready, &pb.SearchFlightsRequest{Origin: "SVO", Destination: "LED"})
	case "verify":
		err = verify(ctx)
	default:
		err = fmt.Errorf("unknown check")
	}
	if err != nil {
		log.Fatal(err)
	}
}

func request(ctx context.Context, method, path string, body any, want int, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, env("BOOKING_URL", "http://127.0.0.1:8080")+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != want {
		return fmt.Errorf("%s %s: got %d, want %d: %s", method, path, resp.StatusCode, want, data)
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

func verify(ctx context.Context) error {
	raw, err := flightClient()
	if err != nil {
		return err
	}
	defer raw.Close()
	client := pb.NewFlightServiceClient(raw)
	wrong := metadata.AppendToOutgoingContext(ctx, "x-api-key", "wrong-test-key")
	calls := []func(context.Context) error{
		func(c context.Context) error {
			_, err := client.SearchFlights(c, &pb.SearchFlightsRequest{})
			return err
		},
		func(c context.Context) error { _, err := client.GetFlight(c, &pb.GetFlightRequest{}); return err },
		func(c context.Context) error { _, err := client.ReserveSeats(c, &pb.ReserveSeatsRequest{}); return err },
		func(c context.Context) error {
			_, err := client.ReleaseReservation(c, &pb.ReleaseReservationRequest{})
			return err
		},
		func(c context.Context) error { _, err := client.UpdateFlight(c, &pb.UpdateFlightRequest{}); return err },
	}
	for _, call := range calls {
		for _, authCtx := range []context.Context{ctx, wrong} {
			if err := call(authCtx); status.Code(err) != codes.Unauthenticated {
				return fmt.Errorf("auth check: %v", err)
			}
		}
	}
	fmt.Println("PASS: all RPCs reject missing or wrong API key")

	conn, err := flightClient(grpc.WithUnaryInterceptor(booking.FlightCalls(os.Getenv("FLIGHT_API_KEY"))))
	if err != nil {
		return err
	}
	defer conn.Close()
	client = pb.NewFlightServiceClient(conn)
	if err := checkReservation(ctx, client); err != nil {
		return err
	}
	if err := checkREST(ctx, client); err != nil {
		return err
	}
	fmt.Println("PASS: all integration checks")
	return nil
}

func checkReservation(ctx context.Context, c pb.FlightServiceClient) error {
	// Race for the last seat on the second demo flight.
	f, err := c.GetFlight(ctx, &pb.GetFlightRequest{FlightId: 2})
	if err != nil {
		return err
	}
	if f.AvailableSeats != 1 {
		return fmt.Errorf("demo flight 2 must have one free seat")
	}
	ids := []string{uuid.NewString(), uuid.NewString()}
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i, id := range ids {
		wg.Add(1)
		go func(i int, id string) {
			defer wg.Done()
			_, errs[i] = c.ReserveSeats(ctx, &pb.ReserveSeatsRequest{FlightId: 2, SeatCount: 1, BookingId: id})
		}(i, id)
	}
	wg.Wait()
	winner := ""
	for i, err := range errs {
		if err == nil {
			winner = ids[i]
			defer c.ReleaseReservation(ctx, &pb.ReleaseReservationRequest{BookingId: ids[i]})
		} else if status.Code(err) != codes.ResourceExhausted {
			return err
		}
	}
	if winner == "" || (errs[0] == nil && errs[1] == nil) {
		return fmt.Errorf("exactly one reserve must succeed: %v", errs)
	}
	req := &pb.ReserveSeatsRequest{FlightId: 2, SeatCount: 1, BookingId: winner}
	if _, err := c.ReserveSeats(ctx, req); err != nil {
		return fmt.Errorf("idempotent reserve: %w", err)
	}
	f, err = c.GetFlight(ctx, &pb.GetFlightRequest{FlightId: 2})
	if err != nil || f.AvailableSeats != 0 {
		return fmt.Errorf("stock after retry: %v %v", f, err)
	}
	release := &pb.ReleaseReservationRequest{BookingId: winner}
	for i := 0; i < 2; i++ {
		if _, err := c.ReleaseReservation(ctx, release); err != nil {
			return err
		}
	}
	f, err = c.GetFlight(ctx, &pb.GetFlightRequest{FlightId: 2})
	if err != nil || f.AvailableSeats != 1 {
		return fmt.Errorf("stock after repeated release: %v %v", f, err)
	}
	fmt.Println("PASS: last-seat race, same booking ID, repeated release")
	return nil
}

type listedFlight struct {
	ID        int64  `json:"id"`
	Available int32  `json:"available_seats"`
	Price     int64  `json:"price_cents"`
	Status    string `json:"status"`
}

func checkREST(ctx context.Context, c pb.FlightServiceClient) error {
	const search = "/flights?origin=SVO&destination=LED&date=2026-04-01"
	var list []listedFlight
	if err := request(ctx, "GET", search, nil, 200, &list); err != nil {
		return err
	}
	if len(list) != 2 || list[0].ID != 1 || list[0].Status != "SCHEDULED" {
		return fmt.Errorf("search: %+v", list)
	}
	before := list[0]
	if before.Available < 2 {
		return fmt.Errorf("demo flight 1 needs two free seats")
	}
	var detail listedFlight
	if err := request(ctx, "GET", "/flights/1", nil, 200, &detail); err != nil {
		return err
	}
	if err := request(ctx, "GET", search, nil, 200, nil); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights/1", nil, 200, nil); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights/99999", nil, 404, nil); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights?origin=SVO&destination=LED&date=2026-02-30", nil, 400, nil); err != nil {
		return err
	}
	fmt.Println("PASS: REST search, detail, date validation and not found")

	user := "verify-" + uuid.NewString()
	body := map[string]any{"user_id": user, "flight_id": 1, "passenger_name": "Demo Passenger", "passenger_email": "demo@example.com", "seat_count": 2}
	var b booking.Booking
	if err := request(ctx, "POST", "/bookings", body, 201, &b); err != nil {
		return err
	}
	defer request(ctx, "POST", "/bookings/"+b.ID+"/cancel", nil, 200, nil)
	if b.TotalPriceCents != before.Price*2 || b.Status != "CONFIRMED" {
		return fmt.Errorf("booking: %+v", b)
	}
	if err := request(ctx, "GET", search, nil, 200, &list); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights/1", nil, 200, &detail); err != nil {
		return err
	}
	if list[0].Available != before.Available-2 || detail.Available != before.Available-2 {
		return fmt.Errorf("stale cache after reserve")
	}
	if err := request(ctx, "GET", "/bookings?user_id="+user, nil, 200, nil); err != nil {
		return err
	}

	changed := before.Price + 1
	if _, err := c.UpdateFlight(ctx, &pb.UpdateFlightRequest{FlightId: 1, PriceCents: &changed}); err != nil {
		return err
	}
	defer c.UpdateFlight(ctx, &pb.UpdateFlightRequest{FlightId: 1, PriceCents: &before.Price})
	if err := request(ctx, "GET", search, nil, 200, &list); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights/1", nil, 200, &detail); err != nil {
		return err
	}
	if list[0].Price != changed || detail.Price != changed {
		return fmt.Errorf("stale cache after update")
	}
	var stored booking.Booking
	if err := request(ctx, "GET", "/bookings/"+b.ID, nil, 200, &stored); err != nil {
		return err
	}
	if stored.TotalPriceCents != b.TotalPriceCents {
		return fmt.Errorf("snapshot price changed")
	}
	if err := request(ctx, "POST", "/bookings/"+b.ID+"/cancel", nil, 200, &stored); err != nil {
		return err
	}
	if stored.Status != "CANCELLED" {
		return fmt.Errorf("cancel: %+v", stored)
	}
	if err := request(ctx, "POST", "/bookings/"+b.ID+"/cancel", nil, 409, nil); err != nil {
		return err
	}
	if err := request(ctx, "GET", search, nil, 200, &list); err != nil {
		return err
	}
	if err := request(ctx, "GET", "/flights/1", nil, 200, &detail); err != nil {
		return err
	}
	if list[0].Available != before.Available || detail.Available != before.Available {
		return fmt.Errorf("stale cache after release")
	}

	failedUser := "failed-" + uuid.NewString()
	body["user_id"], body["seat_count"] = failedUser, 1000
	if err := request(ctx, "POST", "/bookings", body, 409, nil); err != nil {
		return err
	}
	var bookings []booking.Booking
	if err := request(ctx, "GET", "/bookings?user_id="+failedUser, nil, 200, &bookings); err != nil {
		return err
	}
	if len(bookings) != 0 {
		return fmt.Errorf("failed reservation created a booking")
	}
	fmt.Println("PASS: REST booking/cancel, price snapshot, cache invalidation and rejected reserve")
	return nil
}
