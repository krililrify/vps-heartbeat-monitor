// hbm-probe：每 30 秒向控制端发送一次心跳。仅标准库，无第三方依赖。
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func beat(c *http.Client, base string, nodeID int, token string) error {
	body, _ := json.Marshal(map[string]int{"node_id": nodeID})
	req, err := http.NewRequest("POST", base+"/api/heartbeat", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("控制端返回 HTTP %d", resp.StatusCode) // 不输出 token
	}
	return nil
}

func main() {
	base, idStr, token := os.Getenv("HBM_URL"), os.Getenv("HBM_NODE_ID"), os.Getenv("HBM_TOKEN")
	id, err := strconv.Atoi(idStr)
	if base == "" || token == "" || err != nil {
		log.Fatal("缺少 HBM_URL / HBM_NODE_ID / HBM_TOKEN")
	}
	interval := 30 * time.Second
	if v, e := strconv.Atoi(os.Getenv("HBM_INTERVAL")); e == nil && v > 0 {
		interval = time.Duration(v) * time.Second
	}
	client := &http.Client{Timeout: 10 * time.Second}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	time.Sleep(time.Duration(rand.Intn(2000)) * time.Millisecond) // 打散启动瞬间
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		if err := beat(client, base, id, token); err != nil {
			log.Printf("心跳失败，将在下个周期重试: %v", err)
		}
		select {
		case <-t.C:
		case <-stop:
			return
		}
	}
}
