package main

import (
	"context"
	"log"
	"net"
	"os"
	"strings"
	"time"

	pb "flightbooking/generated/flightv1"
	"flightbooking/internal/database"
	"flightbooking/internal/flight"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
)

func main() {
	key := os.Getenv("FLIGHT_API_KEY")
	sentinels := os.Getenv("REDIS_SENTINELS")
	if key == "" || sentinels == "" {
		log.Fatal("FLIGHT_API_KEY and REDIS_SENTINELS are required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	db, err := database.Open(ctx, os.Getenv("DATABASE_URL"), "migrations/flight")
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	redisClient := redis.NewFailoverClient(&redis.FailoverOptions{
		MasterName: "flights", SentinelAddrs: strings.Split(sentinels, ","),
		DialTimeout: 200 * time.Millisecond, ReadTimeout: 200 * time.Millisecond,
		WriteTimeout: 200 * time.Millisecond, MaxRetries: -1,
	})
	defer redisClient.Close()
	listener, err := net.Listen("tcp", ":50051")
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer(grpc.UnaryInterceptor(flight.Auth(key)))
	pb.RegisterFlightServiceServer(server, flight.New(db, redisClient))
	log.Print("Flight gRPC listening on :50051")
	if err := server.Serve(listener); err != nil {
		log.Fatal(err)
	}
}
