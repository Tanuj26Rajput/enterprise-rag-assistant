# Orbital Setup Guide

Product: Orbital 2026.3
Audience: a new workspace administrator setting Orbital up for the first time

## What you will have at the end

A workspace, a connection to one source system, one pipeline that runs on a
schedule, and alerting that tells you when it fails. About 30 minutes.

## Step 1: Create your workspace

Sign in and choose "Create workspace". A workspace is the unit of isolation
in Orbital: pipelines, connections, credentials and members all belong to
exactly one workspace, and nothing is shared between workspaces unless you
explicitly publish it.

Give the workspace a name and a short identifier. The identifier appears in
API URLs and cannot be changed afterwards, so choose something stable:
`acme-analytics` rather than `acme-analytics-new-v2`.

Most organisations want one workspace per team that owns pipelines
independently. Resist the temptation to create one workspace per project;
you will spend the following year moving things between them.

## Step 2: Invite your team

Open Settings, then Members, then Invite. Orbital has four roles:

- **Owner** can do anything, including deleting the workspace and changing
  billing. There must always be at least one Owner.
- **Admin** can manage members, connections and pipelines, but cannot delete
  the workspace or change billing.
- **Member** can create and run pipelines, and can read connections but not
  see their credentials.
- **Viewer** can read pipelines, runs and results. A Viewer cannot change or
  run anything.

Start people at Viewer or Member and raise them when they need it. Roles are
changed in one click, so there is no cost to starting low.

## Step 3: Create a connection

A connection is a named set of credentials for an external system. Go to
Connections, choose New connection, and pick the connector type.

Credentials are encrypted at rest with a workspace-specific key and are
never shown again after you save them. You can replace a credential, but you
cannot read it back; if you need the value later, store it in your own
password manager now.

Test the connection before continuing. The test performs a read-only call
against the source system and reports the exact error if it fails, which is
much quicker than discovering the problem on the first scheduled run.

## Step 4: Build your first pipeline

Go to Pipelines, choose New pipeline, and give it a name.

A pipeline is a sequence of steps. Each step takes the output of the
previous step and produces a new output; a step never modifies its input,
which is what allows Orbital to re-run any step without re-running the ones
before it.

Add a source step pointing at your connection. Add a transform step. Add a
destination step. Choose "Run once" and watch the run view: each step shows
its status, its duration, and the number of rows it produced.

A first run that fails is normal. The run view shows the failing step and
the error, and the "Retry from here" button re-runs only from that step
onwards.

## Step 5: Put it on a schedule

Open the pipeline's Schedule tab. Schedules are expressed either as a simple
interval or as a cron expression, and always run in UTC regardless of your
display time zone. This is deliberate: a schedule that silently shifts by an
hour twice a year is a schedule that is wrong twice a year.

Set a timeout. A pipeline with no timeout can hold its slot indefinitely and
block every subsequent run. The default is three times the median duration
of the last ten runs, which is a reasonable starting point.

Choose the overlap behaviour. "Skip" is the right default: if the previous
run is still going when the next is due, the next run is skipped and noted.
"Queue" is appropriate only when every run must happen.

## Step 6: Set up alerting

Open Settings, then Alerts. At minimum, configure an alert for pipeline
failure and one for a run exceeding its expected duration.

Route alerts to a team channel, not to an individual. An alert that goes to
one person is an alert that is missed whenever that person is on holiday.

## Where to go next

The Admin Configuration Reference covers retention, single sign-on, IP
allow-listing and audit log export. The Integrations Overview lists every
connector and what each one supports.
