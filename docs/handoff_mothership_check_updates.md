# Mothership Handoff: `check_updates` Endpoint

## Overview

LlamaBot instances now check the mothership on page load to see if newer Docker image versions are available. The frontend displays an update banner + badge, and a one-click "Update Now" button handles the pull and restart automatically.

**The LlamaBot side is complete.** The mothership needs one new API endpoint.

---

## Endpoint to Implement

### `POST /api/leonardo/check_updates`

**Auth:** Bearer token (same as all existing `/api/leonardo/*` endpoints)

**Request body:**
```json
{
  "instance_name": "acme-corp",
  "current_versions": {
    "llamabot": "0.5.0",
    "llamapress": "0.4.0h"
  }
}
```

**Response when updates are available:**
```json
{
  "updates_available": true,
  "latest_versions": {
    "llamabot": {
      "version": "0.5.1",
      "notes": "Bug fixes and performance improvements"
    },
    "llamapress": {
      "version": "0.4.1",
      "notes": "Security patch"
    }
  }
}
```

**Response when already up to date:**
```json
{
  "updates_available": false,
  "latest_versions": {
    "llamabot": {
      "version": "0.5.0",
      "notes": null
    },
    "llamapress": {
      "version": "0.4.0h",
      "notes": null
    }
  }
}
```

---

## Implementation Notes

### What determines "latest stable"

The mothership needs a way to track the current stable version for each image. Options:
- A config/settings table with `current_stable_llamabot` and `current_stable_llamapress` fields (simplest)
- An admin UI page to set them when you push a new release
- Read from a versions table that tracks release history

### Comparison logic

`updates_available` should be `true` if **either** `current_versions.llamabot` or `current_versions.llamapress` differs from the mothership's current stable version. Simple string comparison is fine - versions don't need semver parsing since you control what the stable tag is.

### `notes` field

Optional free-text string. Shown to the user in the update confirmation dialog. Can be null. Examples: "Bug fixes", "New planning mode", "Security patch for CVE-XXX".

### Auth pattern

Same Bearer token auth as `lease_renew`, `report_message`, `check_paywall`, etc. The token comes from `instance.json` on the Leonardo instance. Validate with `mothership_api_token` on the `leonardo_instance_jsons` record.

### Error handling

LlamaBot treats any non-200 response or network error as "no updates available" (fail-open). No special error format needed - just use standard Rails error responses.

---

## How LlamaBot Calls This

Reference: `app/services/mothership_client.py` - `check_updates()` method

```python
async def check_updates(self, current_llamabot: str, current_llamapress: str):
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{self.config['mothership_url']}/api/leonardo/check_updates",
            json={
                "instance_name": self.config["instance_name"],
                "current_versions": {
                    "llamabot": current_llamabot,
                    "llamapress": current_llamapress,
                }
            },
            headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
        )
```

Called from `GET /api/check-updates` in `app/routers/api.py`, which the frontend hits on every page load.

---

## What Happens After the Response

When `updates_available: true`, the user sees:
1. A green pulsing badge next to the version number in the sidebar
2. A green banner at the top of the chat: "Update available: LlamaBot 0.5.1, LlamaPress 0.4.1 [Update Now]"
3. Clicking "Update Now" shows a confirmation dialog with the `notes` text
4. On confirm, LlamaBot runs a host script that:
   - `sed` replaces image tags in `docker-compose.yml`
   - `docker compose pull llamabot llamapress`
   - `systemd-run` schedules a container restart

---

## Suggested Rails Implementation

```ruby
# config/routes.rb
namespace :api do
  namespace :leonardo do
    post 'check_updates', to: 'instances#check_updates'
  end
end

# app/controllers/api/leonardo/instances_controller.rb
def check_updates
  instance = authenticate_instance!
  current = params[:current_versions] || {}

  latest = {
    llamabot: { version: Setting.stable_llamabot_version, notes: Setting.llamabot_release_notes },
    llamapress: { version: Setting.stable_llamapress_version, notes: Setting.llamapress_release_notes }
  }

  updates_available = current[:llamabot] != latest[:llamabot][:version] ||
                      current[:llamapress] != latest[:llamapress][:version]

  render json: { updates_available: updates_available, latest_versions: latest }
end
```

The `Setting` model (or whatever you prefer) just needs four fields:
- `stable_llamabot_version` (string, e.g. "0.5.1")
- `stable_llamapress_version` (string, e.g. "0.4.1")
- `llamabot_release_notes` (text, nullable)
- `llamapress_release_notes` (text, nullable)
