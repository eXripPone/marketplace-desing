-- +goose Up
CREATE TABLE airlines (
    code varchar(2) PRIMARY KEY CHECK (code ~ '^[A-Z0-9]{2}$'),
    name text NOT NULL CHECK (length(name) > 0)
);

CREATE TABLE flights (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    flight_number text NOT NULL CHECK (length(flight_number) > 0),
    airline_code varchar(2) NOT NULL REFERENCES airlines(code),
    origin varchar(3) NOT NULL CHECK (origin ~ '^[A-Z]{3}$'),
    destination varchar(3) NOT NULL CHECK (destination ~ '^[A-Z]{3}$'),
    departure_at timestamptz NOT NULL,
    arrival_at timestamptz NOT NULL,
    total_seats integer NOT NULL CHECK (total_seats BETWEEN 1 AND 1000),
    available_seats integer NOT NULL CHECK (available_seats BETWEEN 0 AND total_seats),
    price_cents bigint NOT NULL CHECK (price_cents BETWEEN 1 AND 1000000000000),
    status text NOT NULL CHECK (status IN ('SCHEDULED', 'DEPARTED', 'CANCELLED', 'COMPLETED')),
    CHECK (origin <> destination),
    CHECK (arrival_at > departure_at)
);

CREATE UNIQUE INDEX flights_number_day ON flights(flight_number, ((departure_at AT TIME ZONE 'UTC')::date));
CREATE INDEX flights_search ON flights(origin, destination, departure_at) WHERE status = 'SCHEDULED';

CREATE TABLE seat_reservations (
    booking_id uuid PRIMARY KEY,
    flight_id bigint NOT NULL REFERENCES flights(id),
    seat_count integer NOT NULL CHECK (seat_count BETWEEN 1 AND 1000),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED', 'EXPIRED')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE seat_reservations;
DROP TABLE flights;
DROP TABLE airlines;
