# PolicyKit

## Getting Started
`pip install -r requirements.txt`

Put `CLIENT_SECRET = ""` in settings.py

`python3 manage.py runserver`

Run Migrations

Had issue with LOGGING filename...
Need to change where debug log goes or create folder /var/log/django

For django-jet to run with Django 3.0:

Find `jet/dashboard/models.py` file in django-jet distribution and remove `from django.utils.encoding import python_2_unicode_compatible` line as well as the line `@python_2_unicode_compatible`. Do the same in the `"jet/models.py"` file.

Note: I ran into some issues with permissions, remember to install with sudo if executing requires sudo.