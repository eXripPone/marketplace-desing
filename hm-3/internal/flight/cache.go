package flight

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	pb "flightbooking/generated/flightv1"
	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/proto"
)

type cache struct {
	client *redis.Client
	// Prevent a reader from writing an old DB value back after invalidation.
	mu    sync.Mutex
	dirty map[string]bool
}

func newCache(client *redis.Client) *cache {
	return &cache{client: client, dirty: make(map[string]bool)}
}

func flightKey(id int64) string { return fmt.Sprintf("flight:%d", id) }
func searchKey(origin, destination, day string) string {
	return fmt.Sprintf("search:%s:%s:%s", origin, destination, day)
}

func (c *cache) read(ctx context.Context, key string, result proto.Message, load func() error) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.dirty[key] {
		// If Redis was unavailable during a write, never return the old entry.
		if !c.remove(ctx, key) {
			log.Printf("cache bypass %s (pending invalidation)", key)
			return load()
		}
	}
	lookup, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	value, err := c.client.Get(lookup, key).Bytes()
	cancel()
	if err == nil && proto.Unmarshal(value, result) == nil {
		log.Printf("cache hit %s", key)
		return nil
	}
	log.Printf("cache miss %s", key)
	if err := load(); err != nil {
		return err
	}
	value, err = proto.Marshal(result)
	if err == nil {
		store, stop := context.WithTimeout(ctx, 250*time.Millisecond)
		defer stop()
		if err := c.client.Set(store, key, value, 5*time.Minute).Err(); err != nil {
			log.Printf("cache set %s: %v", key, err)
		}
	}
	return nil
}

func (c *cache) remove(ctx context.Context, keys ...string) bool {
	ctx, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancel()
	err := c.client.Del(ctx, keys...).Err()
	for _, key := range keys {
		if err != nil {
			c.dirty[key] = true
		} else {
			delete(c.dirty, key)
		}
	}
	if err != nil {
		log.Printf("cache invalidation deferred: %v", err)
		return false
	}
	log.Printf("cache invalidated %v", keys)
	return true
}

func (c *cache) invalidate(f *pb.Flight) {
	c.mu.Lock()
	defer c.mu.Unlock()
	day := f.DepartureAt.AsTime().UTC().Format("2006-01-02")
	// Only the exact route, with and without a date, can contain this flight.
	c.remove(context.Background(), flightKey(f.Id), searchKey(f.Origin, f.Destination, day), searchKey(f.Origin, f.Destination, "all"))
}
