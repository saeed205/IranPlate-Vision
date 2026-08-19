"""
Run the app over HTTPS with a self-signed certificate.

Browsers only expose getUserMedia (the phone camera) on a secure origin, so
scanning from a phone on the LAN needs TLS.

Fixed here:
  * ``datetime.utcnow()`` is deprecated in Python 3.12+ and produced a naive
    datetime that newer cryptography releases warn about;
  * the SAN listed only localhost / 127.0.0.1 / 0.0.0.0 — never the LAN address
    the script told you to open on your phone, so the cert never matched the
    host actually used. Detected LAN addresses are now included, and the cert is
    regenerated when the current address is missing from it;
  * ``socket.gethostbyname(gethostname())`` commonly returns 127.0.0.1, so the
    printed URL was useless; the outbound-socket trick is used instead;
  * a bare ``except:`` swallowed everything, including KeyboardInterrupt.
"""
import datetime
import ipaddress
import os
import socket
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(BASE_DIR, 'cert.pem')
KEY_FILE = os.path.join(BASE_DIR, 'key.pem')

HOST = os.environ.get('PLATE_HOST', '0.0.0.0')
PORT = int(os.environ.get('PLATE_PORT', '5000'))


def local_ips():
    """Every usable IPv4 address of this host, primary one first."""
    found = []

    # The primary address is whichever interface would route outbound traffic.
    # No packet is actually sent for a UDP connect().
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        found.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found:
                found.append(addr)
    except socket.gaierror:
        pass

    return [ip for ip in found if not ip.startswith('127.')]


def make_cert(ip_list):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'plate-local')])

    entries = [x509.DNSName('localhost'),
               x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))]
    for ip in ip_list:
        try:
            entries.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
        except ValueError:
            continue
    san = x509.SubjectAlternativeName(entries)

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(KEY_FILE, 'wb') as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(CERT_FILE, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass                      # not supported on every filesystem
    print('Certificate generated: %s | گواهی ساخته شد' % CERT_FILE)


def cert_covers(ip_list):
    """True if the existing cert is valid and already lists every address."""
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        return False
    try:
        with open(CERT_FILE, 'rb') as f:
            cert = x509.load_pem_x509_certificate(f.read())
        expires = cert.not_valid_after_utc
        if expires < datetime.datetime.now(datetime.timezone.utc):
            print('Certificate expired — regenerating | گواهی منقضی شده است')
            return False
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        present = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        missing = [ip for ip in ip_list if ip not in present]
        if missing:
            print('Certificate is missing %s — regenerating '
                  '| گواهی شامل این آدرس‌ها نیست' % ', '.join(missing))
            return False
        return True
    except Exception as exc:      # noqa: BLE001 - any unreadable cert is replaced
        print('Could not read existing certificate (%s) — regenerating' % exc)
        return False


def main():
    ips = local_ips()
    if not cert_covers(ips):
        print('Generating self-signed certificate... | در حال ساخت گواهی self-signed')
        make_cert(ips)

    print('\n  https://localhost:%d' % PORT)
    for ip in ips:
        print('  https://%s:%d  <- open this on your mobile '
              '| این آدرس را روی موبایل باز کنید' % (ip, PORT))
    if not ips:
        print('  (No LAN address detected | آدرس شبکه محلی پیدا نشد)')
    print('\n  Self-signed certs show a "Not Secure" warning; use Advanced -> Proceed.\n'
          '  ممکن است مرورگر هشدار بدهد؛ از Advanced -> Proceed استفاده کنید.\n')

    import app as flask_app
    flask_app.app.run(
        debug=False,
        host=HOST,
        port=PORT,
        ssl_context=(CERT_FILE, KEY_FILE),
        use_reloader=False,
        threaded=True,
    )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped | متوقف شد')
        sys.exit(0)
