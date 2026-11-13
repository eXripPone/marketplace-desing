package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	pb "flightbooking/generated/flightv1"
	"flightbooking/internal/booking"
	"flightbooking/internal/database"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	key, addr := os.Getenv("FLIGHT_API_KEY"), os.Getenv("FLIGHT_ADDR")
	if key == "" || addr == "" {
		log.Fatal("FLIGHT_API_KEY and FLIGHT_ADDR are required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, 30*time.Second)
	db, err := database.Open(startup, os.Getenv("DATABASE_URL"), "migrations/booking")
	cancel()
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	conn, err := grpc.NewClient("passthrough:///"+addr, grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDisableRetry(), grpc.WithUnaryInterceptor(booking.FlightCalls(key)))
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()
	service := booking.New(db, pb.NewFlightServiceClient(conn))
	go service.Recover(ctx)
	server := &http.Server{Addr: ":8080", Handler: service.Handler(), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Print("Booking REST listening on :8080")
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
