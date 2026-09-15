package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

type Config struct {
	ListenAddress     string
	MySQLDSN          string
	ValkeyAddress     string
	CacheEnabled      bool
	CacheTTL          time.Duration
	DBReadDelay       time.Duration
	DBMaxOpenConns    int
	DBMaxIdleConns    int
	DBConnMaxLifetime time.Duration
	DependencyTimeout time.Duration
	ShutdownTimeout   time.Duration
	MaxListLimit      int
}

func Load() (Config, error) {
	cfg := Config{
		ListenAddress:     envString("LISTEN_ADDRESS", ":8080"),
		MySQLDSN:          envString("MYSQL_DSN", "catalog:catalog@tcp(localhost:3306)/catalog?parseTime=true"),
		ValkeyAddress:     envString("VALKEY_ADDRESS", "localhost:6379"),
		CacheTTL:          30 * time.Second,
		DBMaxOpenConns:    10,
		DBMaxIdleConns:    5,
		DBConnMaxLifetime: 5 * time.Minute,
		DependencyTimeout: 3 * time.Second,
		ShutdownTimeout:   10 * time.Second,
		MaxListLimit:      100,
	}

	var err error
	if cfg.CacheEnabled, err = envBool("CACHE_ENABLED", false); err != nil {
		return Config{}, err
	}
	if cfg.CacheTTL, err = envDuration("CACHE_TTL", cfg.CacheTTL); err != nil {
		return Config{}, err
	}
	if cfg.DBReadDelay, err = envDuration("DB_READ_DELAY", 0); err != nil {
		return Config{}, err
	}
	if cfg.DBMaxOpenConns, err = envInt("DB_MAX_OPEN_CONNS", cfg.DBMaxOpenConns); err != nil {
		return Config{}, err
	}
	if cfg.DBMaxIdleConns, err = envInt("DB_MAX_IDLE_CONNS", cfg.DBMaxIdleConns); err != nil {
		return Config{}, err
	}
	if cfg.DBConnMaxLifetime, err = envDuration("DB_CONN_MAX_LIFETIME", cfg.DBConnMaxLifetime); err != nil {
		return Config{}, err
	}
	if cfg.DependencyTimeout, err = envDuration("DEPENDENCY_TIMEOUT", cfg.DependencyTimeout); err != nil {
		return Config{}, err
	}
	if cfg.ShutdownTimeout, err = envDuration("SHUTDOWN_TIMEOUT", cfg.ShutdownTimeout); err != nil {
		return Config{}, err
	}
	if cfg.MaxListLimit, err = envInt("MAX_LIST_LIMIT", cfg.MaxListLimit); err != nil {
		return Config{}, err
	}

	if cfg.DBMaxOpenConns < 1 || cfg.DBMaxIdleConns < 0 || cfg.DBMaxIdleConns > cfg.DBMaxOpenConns {
		return Config{}, fmt.Errorf("invalid database pool settings: idle=%d open=%d", cfg.DBMaxIdleConns, cfg.DBMaxOpenConns)
	}
	if cfg.MaxListLimit < 1 {
		return Config{}, fmt.Errorf("MAX_LIST_LIMIT must be positive")
	}
	if cfg.CacheTTL <= 0 || cfg.DependencyTimeout <= 0 || cfg.ShutdownTimeout <= 0 {
		return Config{}, fmt.Errorf("CACHE_TTL, DEPENDENCY_TIMEOUT, and SHUTDOWN_TIMEOUT must be positive")
	}
	return cfg, nil
}

func envString(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}

func envBool(name string, fallback bool) (bool, error) {
	value, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s: %w", name, err)
	}
	return parsed, nil
}

func envInt(name string, fallback int) (int, error) {
	value, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", name, err)
	}
	return parsed, nil
}

func envDuration(name string, fallback time.Duration) (time.Duration, error) {
	value, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", name, err)
	}
	if parsed < 0 {
		return 0, fmt.Errorf("%s must not be negative", name)
	}
	return parsed, nil
}
