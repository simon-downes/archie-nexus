---
name: terraform
description: >
  Terraform and OpenTofu standards: tofu commands, file layout, block and argument
  order, resource naming, tagging, IAM policies and module versioning. Use when writing,
  changing or reviewing .tf files, scaffolding a Terraform project or module, or passing
  coding standards to a Terraform code reviewer. Not for inspecting live AWS or Scalr
  state (use aws-agent-access or the scalr CLI) or for Python code.
---

# Purpose

The Terraform defaults Simon wants when a project hasn't already settled them. A
repository's own README, CONTRIBUTING and existing modules win: match the collection
you are working in.

# When to Use

- Writing or changing Terraform or OpenTofu code
- Starting a Terraform project or a new module
- Reviewing Terraform changes, or briefing a reviewer subagent on the standards

# When Not to Use

- Reading live infrastructure: use `aws-agent-access` for AWS, or the `scalr` CLI for
  workspaces and runs.
- Language-neutral rules (design, security, configuration): the system prompt's coding
  standards already cover them.

# Commands

Use `tofu`, never `terraform`.

- `tofu fmt` before every commit; CI rejects unformatted code. `tofu fmt -check -diff`
  checks without changing files.
- `tofu validate` after changes. It needs `tofu init`, and `tofu init -backend=false`
  is enough for validation.

# Files

Name every file in lower-kebab-case: `main.tf`, `lambda-processor.tf`.

| File | When | Contents |
| --- | --- | --- |
| `providers.tf` | always | only the `terraform` and `provider` blocks |
| `README.md` | always | what the project or module is for and how to use it |
| `variables.tf` | when there are variables | every `variable` block, nothing else |
| `outputs.tf` | when there are outputs | every `output` block, nothing else |
| `locals.tf` | optional | one `locals` block, nothing else |
| `data.tf` | optional, for many data sources | `data` blocks and at most one `locals` block |

- Pin each provider with `~> <MAJOR>.<MINOR>`. Look up the latest stable release on the
  web first; the version you remember is likely out of date.
- Use variables only for values supplied from outside: `TF_VAR_*` or tfvars files.
- Put resources in `main.tf` for a small project. For a larger one, split by one
  strategy and keep to it: by service (`dynamodb.tf`, `lambda-processor.tf`), by
  component (`api.tf`, `monitoring.tf`) or by layer (`frontend.tf`, `backend.tf`).
  Keep supporting resources, such as IAM roles, in the file of the resource they
  support, and give related files a shared prefix.

# Block structure

- Put a file's single `locals` block first.
- Give each `variable` a `description`, `type`, then optional `default` and
  `validation`, in that order. Give each `output` a `description`.
- Order resource and module arguments like this:
  1. `count` or `for_each`, followed by a blank line
  2. the real arguments
  3. `tags`, last of the real arguments
  4. the meta-arguments `depends_on`, `lifecycle`, then `provider`

```hcl
resource "aws_lambda_function" "processor" {
  for_each = var.environments

  function_name = "${local.namespace}-processor"
  runtime       = "python3.13"
  handler       = "main.handler"
  role          = aws_iam_role.processor.arn

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.events.name
    }
  }

  tags = local.common_tags

  depends_on = [aws_cloudwatch_log_group.processor]

  lifecycle {
    create_before_destroy = true
  }
}
```

# Naming

- Name resources `<application>-<environment-key>-<purpose>` in kebab-case, for
  example `myapp-dev-uploads`. The environment key has three letters (`dev`, `stg`,
  `prd`).
- Outside the default region (eu-west-1), add a four-letter region key after the
  environment: `myapp-dev-use1-uploads` (`use1` for us-east-1, `euw2` for eu-west-2).
- Build the prefix once as `local.namespace`. Use a shorter purpose when an AWS name
  length limit requires it.
- Name log groups the AWS way: `/aws/lambda/<application>-<environment-key>-<name>`.
- Name variables and locals in snake_case, after their purpose.

# Tagging

Tag every resource. The `terraform` tag is always present and holds the repository and
path of the source without the org prefix, for example `platform-core/my-service`.
Search the brain for the organisation's other required tags (such as service, owner or
environment); if nothing covers the repository, ask the user.

- Write tag names in lower-kebab-case (`cost-center`) and tag values in lower case.
- Define tags once in `locals` and apply them to AWS resources through the provider's
  `default_tags`. Tag resources from other providers explicitly.

```hcl
locals {
  common_tags = {
    terraform = "platform-core/my-service"
  }
}

provider "aws" {
  default_tags {
    tags = local.common_tags
  }
}

resource "datadog_monitor" "api_errors" {
  # ...
  tags = [for k, v in local.common_tags : "${k}:${v}"]
}
```

# IAM

- Attach a role's permissions as an inline `aws_iam_role_policy`. Create a separate
  `aws_iam_policy` with `aws_iam_role_policy_attachment` only when several roles share
  the policy.
- Write policy documents with `jsonencode({...})`, not `aws_iam_policy_document` data
  sources.

```hcl
resource "aws_iam_role_policy" "processor" {
  name = "processor-permissions"
  role = aws_iam_role.processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
      Resource = aws_dynamodb_table.events.arn
    }]
  })
}
```

# Modules

Version modules with semver, bumped from the conventional commit type: `major` or a
breaking change bumps MAJOR, `feat` bumps MINOR, and `fix`, `refactor`, `docs` and
`chore` bump PATCH when released.

When creating a new module:

1. Read the collection's README and two or three existing modules, and match their
   layout, naming and versioning.
2. Confirm the primary resource and the starting features. Suggest up to five
   features commonly needed for that resource, and let the user accept or reject each.
3. Agree what is fixed and what is a variable. Hardcode security and organisation
   standards, such as an S3 public access block or IAM auth on Aurora. Expose only
   legitimate variations, such as retention periods or instance sizes.
4. Summarise the scope, features, fixed settings and variables, and wait for approval
   before writing code.
5. Write only the agreed features, then run `tofu fmt` and `tofu validate`.

# Example

**User:** "Add an SQS queue for the processor in platform-core/my-service."

1. Read the project: it splits resources by service, so the queue goes in a new
   `sqs-processor.tf`.
2. Name it `"${local.namespace}-processor"`, where `local.namespace` already gives
   `myapp-prd`. The provider's `default_tags` covers tags.
3. Add the `sqs:SendMessage` permission to the processor's existing inline role policy
   with `jsonencode`, and output the queue URL in `outputs.tf` with a description.
4. Run `tofu fmt` and `tofu validate`, and report both results.
