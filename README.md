# CTFd Queue Challenge

A CTFd plugin that adds a Queue Challenge type. Participants register in a queue and may submit a flag only after they are selected.

The challenge inherits CTFd's standard challenge model and scoring implementation. It supports the usual title, category, description, attribution, connection information, files, flags, logic, maximum attempts, and static/dynamic scoring options.

## Features

- Per-challenge queue configuration: manual or automatic progression, participants per draw, interval, public queue position, estimated wait, and a custom selected-participant message.
- FIFO signup by default, with administrator controls to reorder, add, remove, and revoke participants.
- In automatic mode, a background worker advances due queues and selects the configured number of waiting entries.
- In manual mode, administrators advance a queue explicitly from the CTFd administration page.
- The selected participant receives a CTFd notification when selected. The first waiting participant is notified when they become next in line.
- Server-side access control: the backend checks selection before validating a flag.
- In team mode, a queue slot is reserved per team but selection is bound to the specific user who registered. Other teammates cannot submit as that participant.
- Queue operations and notification records are persisted in CTFd's database.

## Installation

The plugin directory must use a valid Python package name. Use the directory name queue_challenge, not the repository name with hyphens.

From the root of your CTFd installation:

    git clone --branch feat/queue-challenge https://github.com/CyberNight-Efrei/ctfd-queue-plugin.git plugins/queue_challenge

Restart all CTFd web workers after installing or updating the plugin. On startup, the plugin registers the challenge type and creates its database tables through SQLAlchemy.

No additional Python package is required; the plugin uses CTFd's existing Flask, SQLAlchemy, and notification APIs.

## Creating a queue challenge

1. In the admin challenge creator, choose Queue Challenge.
2. Set the normal CTFd challenge fields, flags, and value.
3. Configure progression mode (manual or automatic), batch size, interval in seconds, position visibility, estimated wait visibility, and the plain-text message shown after selection.
4. Save the challenge. In the CTFd Queues admin menu, open it to review and manage the queue.

CTFd's normal static, linear, and logarithmic scoring implementation remains active. As with standard CTFd challenges, scoring options are managed in the challenge editor.

## Administrator controls

The Queues menu provides an overview of all queue challenges. Each challenge detail page shows selected participants, the waiting queue, round number, time since the last draw, progression mode, interval, and batch size.

Administrators can advance the queue, move waiting participants up or down, remove a participant, add a CTFd user by ID, or revoke a selected participant. Revocation does not delete or rewrite CTFd solves.

## Automatic progression and deployments

The plugin starts a small scheduler worker and uses a host/container advisory lock so only one worker per host/container starts the periodic loop. Persisted draw timestamps and database row locking also prevent repeated scheduler checks from drawing twice on databases that support SELECT FOR UPDATE.

For multi-replica production deployments, use a shared transactional database such as MySQL or PostgreSQL. SQLite is intended for local/testing installations and is not recommended for multi-worker or multi-host CTFd deployments.

Automatic progression checks due queues every few seconds; the configured interval is the target cadence, not a hard real-time guarantee. An administrator can manually advance a queue regardless of its configured mode.

## Notifications

Notifications are persisted in CTFd's native notifications table. Live alerts use a separate `queue_notification` event that the plugin filters by recipient user ID before calling CTFd's native toast UI; the standard global `notification` event is deliberately not used for private selection alerts. If server-sent events are disabled or temporarily unavailable, the queue status still reflects selection when the challenge view refreshes. The notification targets the actual registered player, including in team mode.

## Validation

The GitHub Actions workflow checks Python and JavaScript syntax. Before using this plugin in a live competition, test it inside a disposable CTFd deployment with the same CTFd version and database engine you use in production. Verify challenge creation/update, native scoring, users and teams mode, duplicate registration, manual and automatic draws, reorder/add/remove actions, notification delivery, and rejection of flag attempts from unselected users.

The implementation is aligned with CTFd's current challenge plugin APIs, but a live CTFd-plus-database integration run is still required before production use.
