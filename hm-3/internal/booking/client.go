package booking

import (
	"context"
	"log"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func FlightCalls(key string) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		ctx = metadata.AppendToOutgoingContext(ctx, "x-api-key", key)
		var err error
		for attempt := 1; attempt <= 3; attempt++ {
			attemptCtx, cancel := context.WithTimeout(ctx, time.Second)
			err = invoke(attemptCtx, method, req, reply, cc, opts...)
			cancel()
			code := status.Code(err)
			if code != codes.Unavailable && code != codes.DeadlineExceeded {
				return err
			}
			log.Printf("grpc %s attempt %d/3: %s", method, attempt, code)
			if attempt == 3 {
				break
			}
			timer := time.NewTimer(100 * time.Millisecond * time.Duration(1<<(attempt-1)))
			select {
			case <-ctx.Done():
				timer.Stop()
				return status.FromContextError(ctx.Err()).Err()
			case <-timer.C:
			}
		}
		return err
	}
}
