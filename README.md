# Kotak Live Market Browser

Browser/mobile Flask app based on the existing stock & mutual-fund screener.

## Render environment variables
NEO_CONSUMER_KEY
NEO_MOBILE_NUMBER
NEO_UCC
NEO_MPIN

Do not commit real credentials. Current 6-digit TOTP is entered in the browser when starting the WebSocket session.

## Deploy
1. Create a new GitHub repository.
2. Upload the CONTENTS of this folder (app.py must be in repository root).
3. Render -> New Web Service -> connect repository.
4. Build: pip install -r requirements.txt
5. Start: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300
6. Add the four environment variables above.
7. Deploy, open site, enter current TOTP, click Connect Live.

The app uses Kotak WebSocket for streaming after authentication and Kotak REST quotes as fallback.
Historical analysis/screening continues to use the existing engine.
