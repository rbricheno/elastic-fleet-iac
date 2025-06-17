# Elastic Fleet as Code

A set of Python utilities and a defined structure to manage Elastic Agent policies, integrations, and their supporting Elasticsearch components (ingest pipelines, component templates).

## Core Concepts

The system is built around a "Read -> Modify -> Write" loop:

1.  **Discover (`discover_state.py`):** Read the configuration from a running Elastic deployment and "dump" it into a structured, version-controllable directory format.
2.  **Modify (Manual Edit):** Edit human-readable YAML files and JSON fragments to define the desired state of your agent policies.
3.  **Build (`build_from_state.py`):** Apply the state defined in your files to a target Elastic deployment. This process creates or updates agent policies idempotently, meaning it can be run repeatedly without causing errors.

## Directory Structure

A state directory (e.g., `fleet_state_discovered/` or `my_fleet_iac/`) contains the complete definition of your fleet policies.

```
fleet_state_discovered/
│
├── fleet_definition.yaml         # The central source of truth for the entire configuration.
│
├── component_templates/          # Contains JSON definitions for Elasticsearch component templates.
│   ├── custom-mappings.json
│   └── ...
│
├── pipelines/                    # Contains JSON definitions for Elasticsearch ingest pipelines.
│   ├── custom-parser.json
│   └── ...
│
└── integration_fragments/        # Contains reusable JSON snippets for each agent integration.
    ├── system-1.json
    ├── nginx-1.json
    └── custom_logs-app-1.json
```

## The `fleet_definition.yaml` File

This is the main orchestration file. It defines what assets exist and how they are combined into agent policies.

```yaml
# Foundational assets that must exist in Elasticsearch first.
# The build script will apply these in the specified order.
foundational_assets:
  component_templates:
    - some-custom-mappings
  ingest_pipelines:
    - a-custom-parser-pipeline

# Defines "integration bundles" and their dependencies.
# This links a reusable fragment to the pipelines it requires.
integration_definitions:
  system:
    fragment: system-1          # The filename (without .json) in integration_fragments/
  my_app_logs:
    fragment: custom_logs-app-1
    dependencies:
      ingest_pipelines:
        - a-custom-parser-pipeline

# Defines the Agent Policies to be created in Fleet.
# The key for each item is the name of the policy that will be created.
agent_policies:
  "Base Linux Policy":
    description: "Default policy for all Linux servers."
    integrations:
      - system                # Refers to the key in integration_definitions
    # The _discovered_agents field is for informational purposes only and is
    # ignored by the build script. It shows which agents were found using this
    # policy configuration during discovery.
    _discovered_agents:
      - server-1
      - server-2

  "Application Server Policy":
    description: "Policy for servers running the custom application."
    integrations:
      - system
      - my_app_logs
    _discovered_agents:
      - server-2
```

## Scope and Best Practices

This tool is designed to manage the **definition** of your Agent Policies. It does not manage:
*   Agent enrollment.
*   The assignment of individual agents to a policy.

The recommended workflow is:
1.  Use this tool to define and apply your policies (e.g., "Linux Base", "Windows DC", "NGINX Servers") to your Fleet.
2.  Use standard Fleet enrollment tokens to enroll new agents into the appropriate, pre-existing policy.

## Scripts

### Prerequisites

All scripts require Python 3 and the following libraries:

```bash
pip install requests pyyaml
```

You must also set the `ELASTIC_API_KEY` environment variable. This key needs sufficient permissions to manage Fleet and Elasticsearch components.

```bash
export ELASTIC_API_KEY="YourBase64EncodedKeyHere"
```

---

### `discover_state.py`

Connects to an existing Elastic deployment and dumps its entire relevant state into a new directory. This is for creating an initial baseline from a manually configured environment.

**Usage:**
```bash
python discover_state.py --url <kibana_url> [--output-dir <directory_name>]
```
*   `--url`: The base URL of your Kibana instance.
*   `--output-dir`: The directory to save the state files into (defaults to `fleet_state_discovered`).

**What it does:**
1.  Dumps all non-managed component templates and ingest pipelines.
2.  Dumps all unique agent integration configurations into reusable fragments.
3.  Analyzes policies to create logical `agent_policies` definitions based on unique sets of integrations.
4.  Records which agents were assigned to each policy configuration for informational purposes.
5.  Generates a complete `fleet_definition.yaml` that represents the discovered state.

---

### `build_from_state.py`

Reads a state directory and applies its configuration to a target Elastic deployment.

**Usage:**
```bash
python build_from_state.py --url <kibana_url> --state-dir <directory_name> [--dry-run]
```
*   `--url`: The URL of the target Kibana instance.
*   `--state-dir`: The directory containing your `fleet_definition.yaml` and fragment files.
*   `--dry-run`: (Optional) If included, the script will print the `curl` commands for all the changes it *would* make without actually executing them.

**What it does:**
1.  Applies all `component_templates` and `ingest_pipelines` listed in `foundational_assets`.
2.  For each entry in `agent_policies`, it constructs a complete agent policy by combining the appropriate integration fragments.
3.  It then idempotently creates or updates the agent policies in Fleet via the API.

## Example Workflow: Cloning a Deployment's Configuration

1.  **Discover the source deployment:**
    ```bash
    export ELASTIC_API_KEY="<Source_Deployment_Key>"
    python discover_state.py --url https://source.kb.elastic-cloud.com --output-dir my_fleet_config
    ```

2.  **Review and version-control the result:**
    ```bash
    cd my_fleet_config
    # Review fleet_definition.yaml. Notice it defines policies like "Base Linux Policy".
    git init
    git add .
    git commit -m "Initial fleet configuration baseline"
    ```

3.  **Apply to a new, empty deployment:**
    ```bash
    export ELASTIC_API_KEY="<New_Deployment_Key>"
    # First, do a dry run to see the plan.
    python ../build_from_state.py --url https://new.kb.elastic-cloud.com --state-dir . --dry-run
    
    # If the plan looks good, apply it for real.
    python ../build_from_state.py --url https://new.kb.elastic-cloud.com --state-dir .
    ```
This will create the Agent Policies in your new deployment. You can now enroll agents directly into these policies.

## Using the policies (enrolling agents)

Once policies are created, you'll need to configure your agents with an enrollment token that matches the desired Agent Policy.

Getting this token is a two step API call. First you need to find the ID of the Agent Policy based on its name, then you need to request an enrollment token using that Agent Policy ID.

Get the policy like this:

```
# Make sure your Kibana URL and API Key are set
KIBANA_URL="https://your-deployment.kb.elastic-cloud.com"
export ELASTIC_API_KEY="YourBase64EncodedKeyHere"

curl -X GET \
  -H "Authorization: ApiKey $ELASTIC_API_KEY" \
  -H "kbn-xsrf: true" \
  "$KIBANA_URL/api/fleet/agent_policies?perPage=100"
```

It will give you a response with an ID in it like this:
```
{
  "items": [
    // ... other policies
    {
      "id": "01a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",  // <-- This is the policy_id you need!
      "name": "Web Servers",
      "description": "Policy for servers running the Nginx integration.",
      // ... more fields
    }
  ]
}
```

Now use that ID to create an enrollment token:

```
# The policy ID we found in the previous step
POLICY_ID="01a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6"

curl -X POST \
  -H "Authorization: ApiKey $ELASTIC_API_KEY" \
  -H "kbn-xsrf: true" \
  -H "Content-Type: application/json" \
  "$KIBANA_URL/api/fleet/enrollment-api-keys" \
  -d '{
    "name": "Web Servers - Terraform Key",
    "policy_id": "'"$POLICY_ID"'"
  }'
```

The response from the API contains the token:

```
{
  "item": {
    "id": "some-unique-token-id",
    "active": true,
    "policy_id": "01a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "name": "Web Servers - Terraform Key",
    "api_key": "aVeryLongBase64EncodedEnrollmentKeyString...", // <-- This is the token!
    "api_key_id": "another-unique-id",
    "created_on": "2023-10-27T12:00:00.000Z",
    "type": "PERMANENT"
  }
}
```

You can now use that token to enroll your agent:

```
sudo /usr/bin/elastic-agent enroll \
  --url=https://<your-fleet-server-url>:8220 \
  --enrollment-token=aVeryLongBase64EncodedEnrollmentKeyString...
```

## Role descriptors for API keys

You need quite a lot of rights, and a cluster-level API key, to read all the relevant parts of the fleet configuration.

An API key with a role descriptor that looks like this should work for the initial discovery:

```
POST /_security/api_key

{
  "name": "fleet_admin_api_key",
  "role_descriptors": {
    "fleet_manager_global_role": {
      "cluster": ["monitor", "read_pipeline"],
      "indices": [
        {
          "names": ["*"],
          "privileges": ["read", "view_index_metadata"]
        }
      ],
      "applications": [
        {
          "application": "kibana-.kibana",
          "privileges": [
            "feature_fleetv2.all",
            "feature_fleet.all"
          ],
          "resources": ["*"]
        }
      ]
    }
  }
}
```

If you're going to actually build the cluster from an IAC configuration, you need to add in some manage rights, like this:

```
POST /_security/api_key

{
  "name": "fleet_builder_api_key",
  "role_descriptors": {
    "fleet_iac_builder_inline_role": {
      "cluster": [
        "manage_pipeline", 
        "manage_component_template"
      ],
      "kibana": [
        {
          "spaces": [
            "default"
          ],
          "feature": {
            "fleet": [
              "all"
            ]
          },
          "base": []
        }
      ]
    }
  }
}
```

For provisioning enrollment tokens, a Kibana-level API key like this will do the job:

```
POST /_security/api_key

{
  "name": "fleet_enrollment_api_key",
  "role_descriptors": {
    "fleet_token_creator_inline_role": {
      "kibana": [
        {
          "spaces": [
            "default"
          ],
          "feature": {
            "fleet": [
              "all"
            ]
          },
          "base": []
        }
      ]
    }
  }
}
```

Written with help from Google AI studio, thanks google.

License: MIT
