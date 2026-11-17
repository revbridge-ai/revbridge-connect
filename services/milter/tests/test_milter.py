"""
Script that simulates the milter processing an email

Tests the milter locally without connecting to a real Postfix server. It
reads an email from an .eml file or from an environment variable and
simulates every milter callback.

Usage (needs pymilter, i.e. the milter image):
    python services/milter/tests/test_milter.py <file.eml>
    or
    EMAIL_CONTENT="..." python services/milter/tests/test_milter.py
"""

import email
import email.parser
import email.policy
import logging
import os
import sys
from socket import AF_INET

# Add the service root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import after adjusting the path
import Milter
from mail_milter.tracking_milter.milter_server import TrackingMilter

# Logging
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger("test_milter")


class MockMilterSocket:
    """Mock of the milter socket, for local tests"""

    def __init__(self):
        self.headers_added = []
        self.body_replacements = []
        self.reply_code = None
        self.reply_text = None
        self.recipients_added = (
            []
        )  # recipients added through addrcpt

    def getsymval(self, sym):
        """Simulates Postfix's getsymval - returns mocked values"""
        if sym == "i":  # queue_id
            return "TEST12345"
        elif sym == "{client_addr}":
            return "127.0.0.1"
        elif sym == "{client_name}":
            return "localhost"
        elif sym == "{client_resolve}":
            return "localhost"
        return None


class TestMilter(TrackingMilter):
    """Test version of TrackingMilter that needs no real SMTP connection"""

    def __init__(self):
        super().__init__()
        self.mock_socket = MockMilterSocket()
        # Set _actions so that addrcpt() works
        # ADDRCPT = 0x00000008 (Milter library flag)
        import Milter

        self._actions = Milter.ADDRCPT  # allow adding recipients

    def getsymval(self, sym):
        """Uses the mock socket instead of the real one"""
        return self.mock_socket.getsymval(sym)

    def addheader(self, name, value):
        """addheader mock - only records"""
        self.mock_socket.headers_added.append((name, value))
        logger.info(f"[MOCK] Adding header: {name} = {value[:100]}")

    def replacebody(self, new_body):
        """replacebody mock - only records"""
        body_preview = new_body[:200] + "..." if len(new_body) > 200 else new_body
        self.mock_socket.body_replacements.append(body_preview)
        logger.info(f"[MOCK] Replacing body (first 200 chars): {body_preview}")

    def setreply(self, rcode, dsn, text):
        """setreply mock - only records"""
        self.mock_socket.reply_code = (rcode, dsn)
        self.mock_socket.reply_text = text
        logger.info(f"[MOCK] SetReply: {rcode} {dsn} - {text}")

    def addrcpt(self, recipient):
        """addrcpt mock - adds a recipient to the SMTP envelope (equivalent to BCC)"""
        self.mock_socket.recipients_added.append(recipient)
        logger.info(f"[MOCK] Adding shadow recipient (BCC): {recipient}")
        return Milter.CONTINUE


def parse_email_from_file(file_path: str) -> email.message.EmailMessage:
    """Read and parse an .eml file"""
    with open(file_path, "rb") as f:
        msg = email.parser.BytesParser(policy=email.policy.default).parse(f)
    return msg


def parse_email_from_string(email_str: str) -> email.message.EmailMessage:
    """Parse an email from a string"""
    return email.parser.Parser(policy=email.policy.default).parsestr(email_str)


def simulate_milter_callbacks(
    milter: TestMilter, msg: email.message.EmailMessage, email_file: str = None
):
    """
    Simulate every milter callback, in order

    Args:
        milter: TestMilter instance
        msg: Parsed EmailMessage
        email_file: Optional path to the original .eml file (for the raw body)
    """
    logger.info("=" * 80)
    logger.info("STARTING THE MILTER CALLBACK SIMULATION")
    logger.info("=" * 80)

    # 1. CONNECT
    logger.info("\n[1] Simulating CONNECT...")
    result = milter.connect("localhost", AF_INET, "127.0.0.1")
    logger.info(f"CONNECT returned: {result}")

    # 2. ENVFROM (MAIL FROM)
    logger.info("\n[2] Simulating ENVFROM (MAIL FROM)...")
    from_addr = msg.get("From", "sender@example.com")
    # Keep only the email address
    if "<" in from_addr:
        from_addr = from_addr.split("<")[1].split(">")[0]
    result = milter.envfrom(from_addr)
    logger.info(f"ENVFROM({from_addr}) returned: {result}")

    # 3. ENVRCPT (RCPT TO) - for each recipient
    logger.info("\n[3] Simulating ENVRCPT (RCPT TO)...")
    recipients = []

    # Collect every recipient
    for header in ["To", "Cc", "Bcc"]:
        addr_list = msg.get_all(header, [])
        for addr in addr_list:
            if "<" in addr:
                addr = addr.split("<")[1].split(">")[0]
            recipients.append(addr)

    # No recipients: use a default
    if not recipients:
        recipients = ["recipient@example.com"]

    for recipient in recipients:
        result = milter.envrcpt(recipient)
        logger.info(f"ENVRCPT({recipient}) returned: {result}")
        if result == Milter.REJECT:
            logger.warning(f"Email REJECTED for {recipient}")
            return  # stop when rejected

    # 4. HEADERS
    logger.info("\n[4] Simulating HEADERS...")
    for header_name, header_value in msg.items():
        result = milter.header(header_name, header_value)
        logger.debug(f"HEADER({header_name}) returned: {result}")

    logger.info(f"Processed {len(list(msg.items()))} headers")

    # 5. BODY - split into chunks to simulate real processing
    logger.info("\n[5] Simulating BODY...")

    # Use the full raw email body to keep the multipart boundaries
    # (Postfix sends the whole body, not just one part)
    body = b""

    # Take the raw body from the original file, if available
    if email_file and os.path.exists(email_file):
        try:
            with open(email_file, "rb") as f:
                raw_email = f.read()
                # Find the separator between headers and body
                # RFC 822: headers end with \r\n\r\n or \n\n
                sep = raw_email.find(b"\r\n\r\n")
                if sep == -1:
                    sep = raw_email.find(b"\n\n")
                if sep != -1:
                    # Keep only the body (after the headers)
                    body = (
                        raw_email[sep + 4 :]
                        if b"\r\n\r\n" in raw_email[: sep + 6]
                        else raw_email[sep + 2 :]
                    )
                    logger.info(f"Body taken from the raw file: {len(body)} bytes")
        except Exception as e:
            logger.warning(
                f"Error reading the raw body from the file: {e}, using the alternative method"
            )

    # Fallback: rebuild the body from the EmailMessage object
    if not body:
        try:
            # The body as bytes (includes every multipart boundary);
            # as_bytes() keeps the original encoding (base64, quoted-printable, etc.)
            body_bytes = msg.as_bytes()
            # Split headers from body
            sep = body_bytes.find(b"\r\n\r\n")
            if sep == -1:
                sep = body_bytes.find(b"\n\n")
            if sep != -1:
                body = (
                    body_bytes[sep + 4 :]
                    if b"\r\n\r\n" in body_bytes[: sep + 6]
                    else body_bytes[sep + 2 :]
                )
            else:
                # No separator: use the whole content
                body = body_bytes
            logger.info(f"Body rebuilt from the EmailMessage: {len(body)} bytes")
        except Exception as e:
            logger.warning(f"Error rebuilding the body: {e}, using the simple method")
            # Last fallback: the content of a single part only
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body = part.get_payload(decode=True)
                        if isinstance(body, str):
                            body = body.encode("utf-8")
                        break
                if not body:
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True)
                            if isinstance(body, str):
                                body = body.encode("utf-8")
                            break
            else:
                body = msg.get_payload(decode=True)
                if isinstance(body, str):
                    body = body.encode("utf-8")

    if not body:
        body = b"Empty email body"

    # Send in chunks (as Postfix does)
    chunk_size = 8192  # 8KB per chunk
    chunks_sent = 0
    for i in range(0, len(body), chunk_size):
        chunk = body[i : i + chunk_size]
        result = milter.body(chunk)
        chunks_sent += 1
        logger.debug(
            f"BODY chunk {chunks_sent} ({len(chunk)} bytes) returned: {result}"
        )

    logger.info(f"Processed {chunks_sent} body chunks (total: {len(body)} bytes)")

    # 6. EOM (End of Message)
    logger.info("\n[6] Simulating EOM (End of Message)...")
    result = milter.eom()
    logger.info(f"EOM returned: {result}")

    # Final summary
    logger.info("\n" + "=" * 80)
    logger.info("PROCESSING SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Headers added: {len(milter.mock_socket.headers_added)}")
    for name, value in milter.mock_socket.headers_added:
        logger.info(f"  - {name}: {value[:100]}")

    if milter.mock_socket.body_replacements:
        logger.info(
            f"Body replaced: {len(milter.mock_socket.body_replacements)} times"
        )

    if milter.mock_socket.reply_code:
        logger.info(
            f"Reply code: {milter.mock_socket.reply_code[0]} {milter.mock_socket.reply_code[1]}"
        )
        logger.info(f"Reply text: {milter.mock_socket.reply_text}")

    if milter.mock_socket.recipients_added:
        logger.info(
            f"Shadow recipients added (BCC): {len(milter.mock_socket.recipients_added)}"
        )
        for recipient in milter.mock_socket.recipients_added:
            logger.info(f"  - {recipient}")

    logger.info(f"Message ID: {milter.message_id}")
    logger.info(f"Domain ID: {milter.domain_name}")
    logger.info(f"Recipients: {milter.rcpts}")
    logger.info("=" * 80)


def main():
    """Main function"""
    logger.info("Starting the milter test...")

    # Where the email comes from
    email_content = None
    email_file = None

    # 1. From an environment variable
    email_content = os.environ.get("EMAIL_CONTENT")
    if email_content:
        logger.info("Reading the email from the EMAIL_CONTENT environment variable")
        msg = parse_email_from_string(email_content)
        email_file = None  # no file when it comes from an environment variable

    # 2. From a file given as an argument
    elif len(sys.argv) > 1:
        email_file = sys.argv[1]
        logger.info(f"Reading the email from file: {email_file}")
        msg = parse_email_from_file(email_file)

    # 3. From a default file
    else:
        default_files = [
            "test_data/sample_email.eml",
            "tests/sample_email.eml",
            "sample_email.eml",
        ]

        for default_file in default_files:
            if os.path.exists(default_file):
                email_file = default_file
                logger.info(f"Reading the email from the default file: {email_file}")
                msg = parse_email_from_file(email_file)
                break

        if not email_file:
            logger.error("No email given!")
            logger.error("Usage:")
            logger.error("  python services/milter/tests/test_milter.py <file.eml>")
            logger.error("  or")
            logger.error("  EMAIL_CONTENT='...' python services/milter/tests/test_milter.py")
            sys.exit(1)

    # Test milter instance
    milter = TestMilter()

    # Simulate the processing
    try:
        simulate_milter_callbacks(milter, msg, email_file)
        logger.info("\nTest finished successfully!")
    except Exception as e:
        logger.error(f"\nError during the test: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
