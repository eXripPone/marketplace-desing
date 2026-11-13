-- +goose Up
CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    user_id text NOT NULL CHECK (length(user_id) BETWEEN 1 AND 100),
    flight_id bigint NOT NULL CHECK (flight_id > 0),
    passenger_name text NOT NULL CHECK (length(passenger_name) BETWEEN 1 AND 200),
    passenger_email text NOT NULL CHECK (length(passenger_email) BETWEEN 1 AND 254),
    seat_count integer NOT NULL CHECK (seat_count BETWEEN 1 AND 1000),
    total_price_cents bigint NOT NULL CHECK (total_price_cents > 0),
    status text NOT NULL CHECK (status IN ('CONFIRMED', 'CANCELLED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL))
);
CREATE INDEX bookings_user ON bookings(user_id, created_at DESC);

-- Service records for finishing interrupted cross-service operations.
CREATE TABLE flight_operations (
    booking_id uuid PRIMARY KEY,
    action text NOT NULL CHECK (action IN ('cleanup', 'cancel')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE flight_operations;
DROP TABLE bookings;
