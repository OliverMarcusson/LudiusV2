import os

# The fixtures use the Exchange range's domain; a developer .env may set the grommunio range's.
os.environ["LUDIUS_MAIL_DOMAIN"] = "ludius.internal"
# Most fixtures plan the Notepad note as the generic item: a range without Word (planner.installed).
os.environ["LUDIUS_APPS"] = "edge,excel,explorer,notepad,outlook,paint,powerpoint,powershell,wordpad"
