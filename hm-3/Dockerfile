FROM golang:1.24-alpine AS builder
WORKDIR /src
RUN apk add --no-cache protoc protobuf-dev
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.6 &&     go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
COPY . .
RUN protoc -I proto -I /usr/include --go_out=. --go_opt=module=flightbooking     --go-grpc_out=. --go-grpc_opt=module=flightbooking proto/flight.proto
RUN --mount=type=cache,target=/go/pkg/mod go mod download
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build go test ./...
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -o /bin/flight ./cmd/flight &&     CGO_ENABLED=0 go build -o /bin/booking ./cmd/booking &&     CGO_ENABLED=0 go build -o /bin/check ./cmd/check

FROM scratch AS generated
COPY --from=builder /src/go.mod /src/go.sum /
COPY --from=builder /src/generated /generated

FROM alpine:3.22
WORKDIR /app
COPY --from=builder /bin/flight /bin/booking /bin/check /app/
COPY migrations /app/migrations
USER 65534:65534
