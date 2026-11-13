package database

import (
	"context"
	"database/sql"
	"fmt"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
)

func Open(ctx context.Context, url, migrations string) (*sql.DB, error) {
	if url == "" {
		return nil, fmt.Errorf("DATABASE_URL is required")
	}
	db, err := sql.Open("pgx", url)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
	if err = goose.SetDialect("postgres"); err == nil {
		err = goose.UpContext(ctx, db, migrations)
	}
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate %s: %w", migrations, err)
	}
	return db, nil
}
