// Personal WhatsApp connector. The serve process alone owns WhatsApp keys;
// mcp uses a local Unix socket. Text sending requires explicit enablement.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
)

func privateDir(path string) error {
	if !filepath.IsAbs(path) {
		return errors.New("directories must be absolute")
	}
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return errors.New("connector directories must be real, private directories (mode 0700)")
	}
	return nil
}

func writeQR(path, code string) error {
	png, err := qrcode.Encode(code, qrcode.Medium, 384)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".pair-*.png")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(png); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

// managedSend names the host-controlled send transport: a second private socket and the operator-created HMAC secret.
type managedSend struct{ socket, secretFile string }

func (m managedSend) enabled() bool { return m.socket != "" }

func serve(ctx context.Context, dataDir, socket string, pair, pairOnly, enableSend bool, managed managedSend) error {
	if err := privateDir(dataDir); err != nil {
		return err
	}
	if err := privateDir(filepath.Dir(socket)); err != nil {
		return err
	}
	var managedSecret []byte
	if managed.enabled() {
		if err := privateDir(filepath.Dir(managed.socket)); err != nil {
			return err
		}
		secret, err := loadManagedSecret(managed.secretFile)
		if err != nil {
			return err
		}
		managedSecret = secret
	}
	lock, err := os.OpenFile(filepath.Join(dataDir, "reader.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("another reader owns this data directory")
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	qrPath := filepath.Join(dataDir, "pair.png")
	if err = os.Remove(qrPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	defer os.Remove(qrPath)
	idx, err := openIndex(filepath.Join(dataDir, "messages.sqlite3"))
	if err != nil {
		return err
	}
	defer idx.db.Close()
	container, err := sqlstore.New(ctx, "sqlite3", sqliteURL(filepath.Join(dataDir, "session.sqlite3")), waLog.Noop)
	if err != nil {
		return err
	}
	defer container.Close()
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return err
	}
	if device.ID == nil && !pair {
		return errors.New("account is not linked; operator must run serve with --pair")
	}
	if managed.enabled() && device.ID == nil {
		return errors.New("managed sending requires an already linked account; pair it first without managed sending")
	}
	if device.ID != nil {
		if err = idx.bindAccount(ctx, device.ID.ToNonAD().String()); err != nil {
			return err
		}
		if pairOnly {
			log.Print("WhatsApp is already linked; no new pairing requested")
			return nil
		}
	}
	client := whatsmeow.NewClient(device, waLog.Noop)
	client.InitialAutoReconnect = true
	r := &receiver{ctx: ctx, index: idx, client: client, fatal: make(chan error, 1)}
	if enableSend {
		r.writer = &textSender{index: idx, client: client}
	}
	defer r.stop()
	client.AddEventHandler(r.event)
	var qr <-chan whatsmeow.QRChannelItem
	if device.ID == nil {
		qr, err = client.GetQRChannel(ctx)
		if err != nil {
			return err
		}
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return fmt.Errorf("cannot bind reader socket (a stale socket requires operator inspection): %w", err)
	}
	defer listener.Close()
	if err = os.Chmod(socket, 0600); err != nil {
		return err
	}
	server := &http.Server{Handler: connectorHandler(r), ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 30 * time.Second}
	serverErrors := make(chan error, 2)
	go func() { serverErrors <- server.Serve(listener) }()
	defer server.Close()
	if managed.enabled() {
		// The managed sender has its own socket and handler; the read socket above never gains a send path.
		managedListener, err := net.Listen("unix", managed.socket)
		if err != nil {
			return fmt.Errorf("cannot bind managed send socket (a stale socket requires operator inspection): %w", err)
		}
		defer managedListener.Close()
		if err = os.Chmod(managed.socket, 0600); err != nil {
			return err
		}
		managedServer := &http.Server{Handler: managedHandler(newManagedSender(managedSecret, &textSender{index: idx, client: client})),
			ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 30 * time.Second}
		go func() { serverErrors <- managedServer.Serve(managedListener) }()
		defer managedServer.Close()
	}
	if err = client.Connect(); err != nil {
		return err
	}
	defer client.Disconnect()
	log.Printf("Personal WhatsApp connector started; text sending enabled=%t managed=%t; no mark-read interface", enableSend, managed.enabled())
	for {
		select {
		case <-ctx.Done():
			if pairOnly {
				return ctx.Err()
			}
			return nil
		case err := <-r.fatal:
			return err
		case err := <-serverErrors:
			return err
		case item, ok := <-qr:
			if !ok {
				qr = nil
				continue
			}
			switch item.Event {
			case "code":
				if err = writeQR(qrPath, item.Code); err != nil {
					return err
				}
				log.Printf("Scan the private QR image at %s using WhatsApp > Settings > Linked Devices (expires in %s)", qrPath, item.Timeout)
			case "success":
				if err = os.Remove(qrPath); err != nil && !errors.Is(err, os.ErrNotExist) {
					return err
				}
				log.Print("WhatsApp linked; waiting for received history and messages")
				if pairOnly {
					return nil
				}
			default:
				return fmt.Errorf("pairing ended (%s); restart the operator pairing command", item.Event)
			}
		}
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: whatsapp-personal serve --data-dir PATH --socket PATH [--pair | --enable-send | --managed-send-socket PATH --managed-send-secret-file PATH] | mcp --socket PATH")
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	socket := fs.String("socket", "", "absolute local read socket path")
	enableSend := fs.Bool("enable-send", false, "enable plain-text sending to existing indexed conversations")
	var dataDir, managedSocket, managedSecret *string
	var pair *bool
	var pairOnly *bool
	if args[0] == "serve" {
		dataDir = fs.String("data-dir", "", "private persistent directory outside source/release trees")
		pair = fs.Bool("pair", false, "operator-only QR pairing when unlinked")
		pairOnly = fs.Bool("pair-only", false, "exit after pairing; requires --pair")
		managedSocket = fs.String("managed-send-socket", "", "absolute private socket for host-managed, authenticated sending; exclusive with --enable-send")
		managedSecret = fs.String("managed-send-secret-file", "", "absolute operator-created HMAC secret file (mode 0600) for --managed-send-socket")
	} else if args[0] != "mcp" {
		return errors.New("expected serve or mcp")
	}
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if fs.NArg() != 0 || !filepath.IsAbs(*socket) {
		return errors.New("an absolute --socket path is required; positional arguments are not accepted")
	}
	if args[0] == "mcp" {
		return newMCPServer(*socket, *enableSend).Run(ctx, &mcp.StdioTransport{})
	}
	if *pairOnly && !*pair {
		return errors.New("--pair-only requires --pair")
	}
	if *pairOnly && *enableSend {
		return errors.New("--pair-only cannot enable sending")
	}
	managed := managedSend{socket: *managedSocket, secretFile: *managedSecret}
	if (managed.socket == "") != (managed.secretFile == "") {
		return errors.New("--managed-send-socket and --managed-send-secret-file are required together")
	}
	if managed.enabled() {
		if *enableSend {
			return errors.New("--managed-send-socket cannot be combined with --enable-send")
		}
		if *pair || *pairOnly {
			return errors.New("managed sending cannot be combined with pairing")
		}
		if !filepath.IsAbs(managed.socket) || !filepath.IsAbs(managed.secretFile) || filepath.Clean(managed.socket) == filepath.Clean(*socket) {
			return errors.New("managed send needs its own absolute socket and an absolute secret file path")
		}
	}
	return serve(ctx, *dataDir, *socket, *pair, *pairOnly, *enableSend, managed)
}

func main() {
	// Must run before opening session/index databases or binding the socket.
	syscall.Umask(0077)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, os.Args[1:]); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}
