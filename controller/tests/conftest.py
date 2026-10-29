import os

# The fixtures use the Exchange range's domain; a developer .env may set the grommunio range's.
os.environ["LUDIUS_MAIL_DOMAIN"] = "ludius.internal"
