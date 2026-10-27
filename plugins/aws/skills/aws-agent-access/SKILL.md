---
name: aws-agent-access
description: >
  Read-only access to AWS accounts through the AWS CLI profiles Archie generates at session start: list and
  choose profiles, run aws commands across accounts, use the profiles from scripts and SDKs, and handle
  AccessDenied, DenyDataPlaneReads and expired credentials. Use when asked to inspect, query or debug AWS
  infrastructure, or when an aws command fails. Not for changing AWS resources (the roles are read-only) or
  for editing ~/.aws/config (it is generated).
---

# How access works

- The user logs in on the host with `aws-vault exec <profile> -- archie auth login aws`. Those temporary base
  credentials stay in Archie's credential store; they are not in your environment. They can only assume the
  configured roles.
- At session start Archie writes `~/.aws/config` from `plugins.aws.settings.profiles` (profile name → role ARN).
  Each role profile uses `source_profile = archie-base`. The `archie-base` profile reads the stored base
  credentials through `credential_process` on every use, so a new host login works in this session without a
  restart.
- Never edit `~/.aws/config`; it is container-local and overwritten on every start. To change profiles, the user
  edits `plugins.aws.settings.profiles` in their Archie config and starts a new session.
- `~/.aws` must not be mounted into the container when `plugins.aws.settings.profiles` is set; Archie refuses to
  start rather than overwrite a host-mounted or hand-written `~/.aws/config`.

# Principles

- **Use a role profile for every call.** Never use `archie-base` directly; it is only the source for the roles.
  A call without a profile fails because no credentials are in the environment.
- **Denials are design.** Don't retry with another profile or API to get around an `AccessDenied`.
- **Everything you read is sent to the model provider.** Read only what the task needs.

# Workflow

1. List profiles; the names show the account (ignore `archie-base`). Choose one per account:
   ```bash
   aws configure list-profiles
   aws sts get-caller-identity --profile <profile>   # check the role works
   ```
2. Run commands with that profile. Pass `--region` for other regions or global services:
   ```bash
   aws ec2 describe-instances --profile <profile> --query 'Reservations[].Instances[].InstanceId'
   AWS_PROFILE=<profile> aws rds describe-db-instances      # equivalent
   ```
3. Across accounts, loop over the profiles and label each result with its profile:
   ```bash
   for p in $(aws configure list-profiles | grep -vx archie-base); do echo "== $p"
     aws lambda list-functions --profile "$p" --query 'Functions[].FunctionName' --output text; done
   ```
   Prefer `--query`, `--output text|table` and `describe-*`/`list-*` calls to keep output small.
4. From code, use the profile, not raw credentials. The SDK assumes and refreshes the role:
   ```python
   import boto3
   session = boto3.Session(profile_name="<profile>")
   ec2 = session.client("ec2")
   ```
   Other SDKs and tools: set `AWS_PROFILE=<profile>` (and optionally `AWS_REGION`).

# Failures

| Symptom | Meaning | Action |
|---|---|---|
| `AccessDenied` on `sts:AssumeRole` | The base identity may not assume that role, or the role is missing | Tell the user. Don't try other profiles |
| `explicit deny ... DenyDataPlaneReads` | Data reads are blocked by design (S3 objects, secret values, DynamoDB items, SQS messages, KMS decrypt...) | Use metadata calls instead. Don't work around it |
| `ExpiredToken`, or `archie aws credentials: ...` in the error | The host login ended or was never done | Ask the user to run `aws-vault exec <profile> -- archie auth login aws` on the host, then retry. No new session is needed |
| `AccessDenied` on another action | The read-only policy doesn't include it | Report the action to the user |
| `aws configure list-profiles` shows nothing, or only `archie-base` | No profiles configured | Ask the user to add `plugins.aws.settings.profiles` to their Archie config |

# Sensitive data

Some permitted reads still return secrets or personal data: Lambda environment variables
(`lambda get-function-configuration`), EC2 user data and launch template versions, log events, API Gateway API
keys and stage variables, WAF sampled requests, Performance Insights SQL text. Only call these when the task
needs them, read only what is required, and never repeat secret values in output, files, commits or tickets:
name the variable or field instead.

Never print, log or write credentials to files, pass them as command-line arguments, or export them
(`aws configure export-credentials`) unless a tool cannot use profiles, and then only in a subshell.
