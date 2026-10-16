package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBeat(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/heartbeat" || r.Header.Get("Authorization") != "Bearer tok" {
			w.WriteHeader(401)
			return
		}
		w.WriteHeader(200)
	}))
	defer srv.Close()
	c := &http.Client{Timeout: time.Second}
	if err := beat(c, srv.URL, 1, "tok"); err != nil {
		t.Fatal(err)
	}
	if err := beat(c, srv.URL, 1, "bad"); err == nil {
		t.Fatal("错误 token 应失败")
	} else if bytesContains(err.Error(), "bad") {
		t.Fatal("错误信息不应包含 token")
	}
	srv.Close()
	if err := beat(c, srv.URL, 1, "tok"); err == nil {
		t.Fatal("服务不可达应返回错误而不是 panic")
	}
}

func bytesContains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
