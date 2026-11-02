# Football calendar feed

Publishes one fussball.de team's match schedule as a token-protected iCalendar subscription. Updates every six hours and keeps the last successful calendar in a Docker volume.

## Deploy

Copy `compose.yaml` and `.env.example` to the Docker host, then run `cp .env.example .env`. Set `TEAM_ID` (from the fussball.de team URL) and `CALENDAR_IMAGE`. Generate `CALENDAR_TOKEN` with:

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

Add the generated token to `.env` and start the service:

   ```sh
   docker compose config --quiet
   docker compose pull
   docker compose up -d
   ```

Subscribe at `https://YOUR-DOMAIN/calendar.ics?token=YOUR-TOKEN`.

## Development

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```
