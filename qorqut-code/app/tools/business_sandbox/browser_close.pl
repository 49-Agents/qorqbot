#!/usr/bin/perl
# Loopback-only CDP Browser.close.  This image-local helper keeps the private
# Chrome listener private; it accepts a port, never a host or arbitrary URL.
use strict;
use warnings;
use IO::Socket::INET;

my $port = shift;
die "expected one unprivileged port\n" if @_ || !defined($port) || $port !~ /\A[1-9][0-9]{3,4}\z/ || $port > 65535;
alarm 10;

sub socket_to_chrome {
    my $socket = IO::Socket::INET->new(PeerAddr => "127.0.0.1", PeerPort => $port, Proto => "tcp", Timeout => 5);
    die "Chrome loopback endpoint is unavailable\n" if !defined($socket);
    return $socket;
}

sub read_http {
    my ($socket) = @_;
    my $headers = "";
    while ($headers !~ /\r\n\r\n/) {
        my $part;
        my $count = sysread($socket, $part, 4096);
        die "Chrome version endpoint closed early\n" if !defined($count) || $count == 0;
        $headers .= $part;
        die "Chrome response headers are too large\n" if length($headers) > 8192;
    }
    my ($head, $body) = split(/\r\n\r\n/, $headers, 2);
    my ($length) = $head =~ /^Content-Length:\s*([0-9]+)\s*$/im;
    die "Chrome version response has no bounded body\n" if !defined($length) || $length > 65536;
    while (length($body) < $length) {
        sysread($socket, my $part, $length - length($body)) or die "Chrome version body closed early\n";
        $body .= $part;
    }
    return ($head, $body);
}

sub base64 {
    my ($value) = @_;
    my @alphabet = split(//, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/");
    my $encoded = "";
    while (length($value)) {
        my $block = substr($value, 0, 3, "");
        my @bytes = unpack("C*", $block);
        my $number = (($bytes[0] // 0) << 16) | (($bytes[1] // 0) << 8) | ($bytes[2] // 0);
        $encoded .= $alphabet[($number >> 18) & 63] . $alphabet[($number >> 12) & 63]
            . (length($block) > 1 ? $alphabet[($number >> 6) & 63] : "=")
            . (length($block) > 2 ? $alphabet[$number & 63] : "=");
    }
    return $encoded;
}

my $http = socket_to_chrome();
print {$http} "GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:$port\r\nConnection: close\r\n\r\n";
my ($headers, $body) = read_http($http);
die "Chrome version endpoint rejected request\n" if !defined($body) || $headers !~ m{\AHTTP/1\.[01] 200 };
my ($endpoint) = $body =~ /"webSocketDebuggerUrl"\s*:\s*"([^"]+)"/;
my ($path) = defined($endpoint) && $endpoint =~ m{\Aws://127\.0\.0\.1:\Q$port\E(/devtools/browser/[A-Za-z0-9._~!%()+,;=:@/?'_*.-]+)\z};
die "Chrome returned an invalid private debugger endpoint\n" if !defined($path);

my $socket = socket_to_chrome();
open(my $random, "<:raw", "/dev/urandom") or die "random source unavailable\n";
read($random, my $mask, 4) == 4 or die "random source unavailable\n";
my $key = base64($mask . $mask . $mask . $mask);
print {$socket} "GET $path HTTP/1.1\r\nHost: 127.0.0.1:$port\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n\r\n";
my $response = "";
while ($response !~ /\r\n\r\n/) {
    sysread($socket, my $part, 1) == 1 or die "Chrome websocket handshake failed\n";
    $response .= $part;
    die "Chrome websocket handshake is too large\n" if length($response) > 8192;
}
die "Chrome websocket upgrade failed\n" if $response !~ m{\AHTTP/1\.1 101 };

my $payload = '{"id":1,"method":"Browser.close"}';
die "CDP request is unexpectedly large\n" if length($payload) >= 126;
my $masked = join("", map { chr(ord(substr($payload, $_, 1)) ^ ord(substr($mask, $_ % 4, 1))) } 0 .. length($payload) - 1);
print {$socket} pack("CC", 0x81, 0x80 | length($payload)) . $mask . $masked;

# Chrome may close the socket just after returning the result.  Reaching the
# accepted private endpoint and writing the exact method is the command result;
# the Python lifecycle waits for all Chrome processes before unlocking.
exit 0;
