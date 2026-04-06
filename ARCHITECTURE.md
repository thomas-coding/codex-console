# Architecture

## Runtime Layout

- Web application entrypoint runs inside Docker container `app_webui_1`.
- Source tree under `/opt/codex-console/app` is the Docker build context used for production rebuilds.
- Persistent data lives under `/opt/codex-console/app/data`, including `database.db`.
- App listens on container port `1455`; upstream HTTP may redirect before reaching the app directly.

## Relevant Modules

- `src/web/routes/email.py`: email-service CRUD, Outlook batch import/delete, route-level hotfixes.
- `src/database/models.py`: SQLAlchemy models including `EmailService` and `RegistrationTask`.
- `src/database/session.py`: DB session manager, SQLite setup, migrations.
- `static/js/email_services.js`: frontend calls for email service create/update/delete actions.

## Deletion Constraint

- `registration_tasks.email_service_id` references `email_services.id`.
- The production schema currently behaves like `NO ACTION` on delete.
- Safe application behavior is:
  - reject delete when active tasks still reference the service
  - clear historical task references
  - delete the service record
