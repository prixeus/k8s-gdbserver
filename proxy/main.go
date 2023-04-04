package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
)

// docker run -v $PWD/proxy:/proxy -w /proxy -e CGO_ENABLED=0 golang:1.19.1-alpine3.16 go build -ldflags="-extldflags=-static"

var (
	srcPort = flag.String("srcport", "8080", "source port")
	dstPort = flag.String("dstport", "9090", "destination port")
	healthURL = flag.String("healthURL", "/health", "health check url")
	scheme = flag.String("scheme", "http", "http or https")
)

func Proxy(w http.ResponseWriter, r *http.Request) {
	log.Println("proxy")
	host := strings.Split(r.Host, ":")[0]

	// Kludge the URI with the new port
	r.RequestURI = ""
	r.URL.Scheme = *scheme
	r.URL.Host = host + ":" + *dstPort

	client := &http.Client{}
	resp, err := client.Do(r)

	if err != nil {
		log.Println("Error happened during proxy call", err)
		return
	}

	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Println("Error happened during read proxy response", err)
		return
	}

	w.WriteHeader(resp.StatusCode)
	w.Write(body)
}

func main() {
	flag.Parse()

	http.HandleFunc(*healthURL, func(w http.ResponseWriter, r *http.Request) {
		log.Println("health")
		fmt.Fprint(w, "OK\n")})
	http.HandleFunc("/",Proxy)

	log.Println("Listening on " + *srcPort)

	log.Fatal(http.ListenAndServe(":" + *srcPort, nil))
}