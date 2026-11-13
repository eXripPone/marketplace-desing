-- +goose Up
INSERT INTO airlines(code, name) VALUES ('SU', 'Аэрофлот'), ('FV', 'Россия');

INSERT INTO flights(flight_number, airline_code, origin, destination, departure_at, arrival_at, total_seats, available_seats, price_cents, status)
VALUES
  ('SU1001', 'SU', 'SVO', 'LED', '2026-04-01 08:00Z', '2026-04-01 09:30Z', 12, 12, 450000, 'SCHEDULED'),
  ('SU1002', 'SU', 'SVO', 'LED', '2026-04-01 12:00Z', '2026-04-01 13:35Z', 1, 1, 620000, 'SCHEDULED'),
  ('FV1003', 'FV', 'VKO', 'LED', '2026-04-02 15:00Z', '2026-04-02 16:35Z', 30, 30, 500000, 'SCHEDULED'),
  ('SU1004', 'SU', 'SVO', 'LED', '2026-04-01 18:00Z', '2026-04-01 19:30Z', 10, 10, 470000, 'CANCELLED'),
  ('SU1001', 'SU', 'SVO', 'LED', '2026-04-02 08:00Z', '2026-04-02 09:30Z', 12, 12, 480000, 'SCHEDULED');

-- +goose Down
DELETE FROM flights WHERE flight_number IN ('SU1001','SU1002','FV1003','SU1004');
DELETE FROM airlines WHERE code IN ('SU','FV');
