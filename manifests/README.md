# Fallback manifests

An application normally ships its own `homeserver.env` in its repository root.
If it cannot, put a fallback manifest here named `<repo-directory>.homeserver.env`.
Deploy uses the fallback only when the application repository has no manifest.
The application repository manifest always wins.

See [examples/example-app.homeserver.env](../examples/example-app.homeserver.env) for the format.
