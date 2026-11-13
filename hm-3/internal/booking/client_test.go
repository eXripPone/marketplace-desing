package booking

import (
	"context"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestFlightCalls(t *testing.T) {
	cases := []struct {
		name     string
		errors   []codes.Code
		attempts int
		final    codes.Code
	}{
		{"success", []codes.Code{codes.OK}, 1, codes.OK},
		{"retry then success", []codes.Code{codes.Unavailable, codes.DeadlineExceeded, codes.OK}, 3, codes.OK},
		{"retry limit", []codes.Code{codes.Unavailable}, 3, codes.Unavailable},
		{"not found", []codes.Code{codes.NotFound}, 1, codes.NotFound},
		{"invalid argument", []codes.Code{codes.InvalidArgument}, 1, codes.InvalidArgument},
		{"no seats", []codes.Code{codes.ResourceExhausted}, 1, codes.ResourceExhausted},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			calls := 0
			err := FlightCalls("test-key")(context.Background(), "/test", nil, nil, nil,
				func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
					md, _ := metadata.FromOutgoingContext(ctx)
					if values := md.Get("x-api-key"); len(values) != 1 || values[0] != "test-key" {
						t.Fatal("missing API key")
					}
					if _, ok := ctx.Deadline(); !ok {
						t.Fatal("attempt has no deadline")
					}
					code := tt.errors[min(calls, len(tt.errors)-1)]
					calls++
					return status.Error(code, "test")
				})
			if calls != tt.attempts || status.Code(err) != tt.final {
				t.Fatalf("calls=%d, code=%s", calls, status.Code(err))
			}
		})
	}
}
