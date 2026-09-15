package catalog

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

type RedisCache struct {
	client            redis.UniversalClient
	ttl               time.Duration
	dependencyTimeout time.Duration
	metrics           Metrics
}

func NewRedisCache(client redis.UniversalClient, ttl, dependencyTimeout time.Duration, metrics Metrics) *RedisCache {
	return &RedisCache{
		client:            client,
		ttl:               ttl,
		dependencyTimeout: dependencyTimeout,
		metrics:           metrics,
	}
}

func (c *RedisCache) GetList(ctx context.Context, limit int) ([]Product, bool, error) {
	var products []Product
	found, err := c.getJSON(ctx, "list", fmt.Sprintf("catalog:products:limit:%d", limit), &products)
	return products, found, err
}

func (c *RedisCache) SetList(ctx context.Context, limit int, products []Product) error {
	return c.setJSON(ctx, "list_set", fmt.Sprintf("catalog:products:limit:%d", limit), products)
}

func (c *RedisCache) GetProduct(ctx context.Context, id int64) (Product, bool, error) {
	var product Product
	found, err := c.getJSON(ctx, "get", fmt.Sprintf("catalog:product:%d", id), &product)
	return product, found, err
}

func (c *RedisCache) SetProduct(ctx context.Context, product Product) error {
	return c.setJSON(ctx, "get_set", fmt.Sprintf("catalog:product:%d", product.ID), product)
}

func (c *RedisCache) getJSON(ctx context.Context, operation, key string, destination any) (bool, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, c.dependencyTimeout)
	defer cancel()
	value, err := c.client.Get(ctx, key).Bytes()
	if err == redis.Nil {
		c.observe(operation, "miss", start)
		return false, nil
	}
	if err != nil {
		c.observe(operation, "error", start)
		return false, err
	}
	if err := json.Unmarshal(value, destination); err != nil {
		c.observe(operation, "error", start)
		return false, err
	}
	c.observe(operation, "hit", start)
	return true, nil
}

func (c *RedisCache) setJSON(ctx context.Context, operation, key string, value any) error {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, c.dependencyTimeout)
	defer cancel()
	encoded, err := json.Marshal(value)
	if err != nil {
		c.observe(operation, "error", start)
		return err
	}
	if err := c.client.Set(ctx, key, encoded, c.ttl).Err(); err != nil {
		c.observe(operation, "error", start)
		return err
	}
	c.observe(operation, "success", start)
	return nil
}

func (c *RedisCache) observe(operation, outcome string, start time.Time) {
	if c.metrics != nil {
		c.metrics.ObserveDependency("valkey", operation, outcome, time.Since(start))
	}
}
