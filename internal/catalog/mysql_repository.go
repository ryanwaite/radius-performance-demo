package catalog

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

type MySQLRepository struct {
	db                *sql.DB
	readDelay         time.Duration
	dependencyTimeout time.Duration
	metrics           Metrics
}

func NewMySQLRepository(db *sql.DB, readDelay, dependencyTimeout time.Duration, metrics Metrics) *MySQLRepository {
	return &MySQLRepository{
		db:                db,
		readDelay:         readDelay,
		dependencyTimeout: dependencyTimeout,
		metrics:           metrics,
	}
}

func (r *MySQLRepository) List(ctx context.Context, limit int) ([]Product, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, r.dependencyTimeout)
	defer cancel()
	conn, err := r.db.Conn(ctx)
	if err != nil {
		r.observe("list", "error", start)
		return nil, err
	}
	defer conn.Close()
	if err := wait(ctx, r.readDelay); err != nil {
		r.observe("list", "error", start)
		return nil, err
	}

	rows, err := conn.QueryContext(ctx, `
		SELECT id, name, description, price_cents
		FROM products
		ORDER BY id
		LIMIT ?`, limit)
	if err != nil {
		r.observe("list", "error", start)
		return nil, err
	}
	defer rows.Close()

	products := make([]Product, 0, limit)
	for rows.Next() {
		var product Product
		if err := rows.Scan(&product.ID, &product.Name, &product.Description, &product.PriceCents); err != nil {
			r.observe("list", "error", start)
			return nil, err
		}
		products = append(products, product)
	}
	if err := rows.Err(); err != nil {
		r.observe("list", "error", start)
		return nil, err
	}
	r.observe("list", "success", start)
	return products, nil
}

func (r *MySQLRepository) Get(ctx context.Context, id int64) (Product, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, r.dependencyTimeout)
	defer cancel()
	conn, err := r.db.Conn(ctx)
	if err != nil {
		r.observe("get", "error", start)
		return Product{}, err
	}
	defer conn.Close()
	if err := wait(ctx, r.readDelay); err != nil {
		r.observe("get", "error", start)
		return Product{}, err
	}

	var product Product
	err = conn.QueryRowContext(ctx, `
		SELECT id, name, description, price_cents
		FROM products
		WHERE id = ?`, id).
		Scan(&product.ID, &product.Name, &product.Description, &product.PriceCents)
	if errors.Is(err, sql.ErrNoRows) {
		r.observe("get", "not_found", start)
		return Product{}, ErrNotFound
	}
	if err != nil {
		r.observe("get", "error", start)
		return Product{}, err
	}
	r.observe("get", "success", start)
	return product, nil
}

func (r *MySQLRepository) observe(operation, outcome string, start time.Time) {
	if r.metrics != nil {
		r.metrics.ObserveDependency("mysql", operation, outcome, time.Since(start))
	}
}

func wait(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
