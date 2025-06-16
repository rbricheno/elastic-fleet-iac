#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import shlex
import sys

import requests
import yaml

# --- API Helper Functions ---
def get_api_key() -> str:
    api_key = os.getenv("ELASTIC_API_KEY")
    if not api_key:
        sys.exit("❌ ELASTIC_API_KEY environment variable is not set.")
    return api_key

def make_api_request(method: str, url: str, api_key: str, dry_run: bool = False, **kwargs) -> requests.Response | None:
    headers = {"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "true", "Content-Type": "application/json"}
    
    if dry_run:
        print(f"      DRY RUN: Would execute {method.upper()} {url}")
        json_body = kwargs.get("json")
        if json_body:
            # Clean up for printing, remove purely informational fields
            body_to_print = {k: v for k, v in json_body.items() if not k.startswith('_')}
            curl_command = (
                f"curl -X {method.upper()} \\\n"
                f"  -H \"Authorization: ApiKey $ELASTIC_API_KEY\" \\\n"
                f"  -H \"kbn-xsrf: true\" \\\n"
                f"  -H \"Content-Type: application/json\" \\\n"
                f"  \"{url}\" \\\n"
                f"  -d {shlex.quote(json.dumps(body_to_print, indent=2))}"
            )
            print("      CURL equivalent:")
            print(curl_command)
        return None

    try:
        response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as exc:
        print(f"❌ API Error: {method.upper()} {url} failed: {exc}")
        if exc.response is not None:
            print(f"   Response: {exc.response.text}")
        sys.exit(1)

# --- Build Functions ---
def apply_foundational_assets(es_url: str, api_key: str, state_dir: pathlib.Path, assets_config: dict, dry_run: bool):
    print("🏗️  Applying foundational assets...")
    # Apply component templates
    templates_dir = state_dir / "component_templates"
    for template_name in assets_config.get("component_templates", []):
        path = templates_dir / f"{template_name}.json"
        if not path.exists():
            print(f"   ⚠️  Warning: Component template file not found, skipping: {path}")
            continue
        print(f"   -> Planning to apply component template: {template_name}")
        with open(path, 'r') as f:
            template_body = json.load(f)
        make_api_request("PUT", f"{es_url}/_component_template/{template_name}", api_key, json=template_body, dry_run=dry_run)

    # Apply ingest pipelines
    pipelines_dir = state_dir / "pipelines"
    for pipeline_name in assets_config.get("ingest_pipelines", []):
        path = pipelines_dir / f"{pipeline_name}.json"
        if not path.exists():
            print(f"   ⚠️  Warning: Ingest pipeline file not found, skipping: {path}")
            continue
        print(f"   -> Planning to apply ingest pipeline: {pipeline_name}")
        with open(path, 'r') as f:
            pipeline_body = json.load(f)
        make_api_request("PUT", f"{es_url}/_ingest/pipeline/{pipeline_name}", api_key, json=pipeline_body, dry_run=dry_run)
    
    if not dry_run:
        print("   ✅ Foundational assets applied.")

def generate_and_apply_agent_policies(kibana_url: str, api_key: str, config: dict, state_dir: pathlib.Path, dry_run: bool):
    """Generates and idempotently applies agent policies from the agent_policies block."""
    print("\n📜 Generating and applying agent policies...")

    policies_config = config.get("agent_policies", {})
    definitions = config.get("integration_definitions", {})
    fragments_dir = state_dir / "integration_fragments"

    if not policies_config:
        print("   -> No agent policies defined in YAML. Skipping.")
        return
    if not definitions:
        print("   -> No integration definitions found in YAML. Skipping policy generation.")
        return

    # Fetch existing policies for idempotency
    print("   -> Fetching existing policies to determine create vs. update...")
    policy_name_to_id = {}
    get_policies_url = f"{kibana_url}/api/fleet/agent_policies?perPage=5000"
    # For GET requests, we don't want to dry-run, we need the data to plan.
    try:
        existing_policies_resp = make_api_request("GET", get_policies_url, api_key, dry_run=False) 
        if existing_policies_resp:
            policy_name_to_id = {p["name"]: p["id"] for p in existing_policies_resp.json().get("items", [])}
    except SystemExit:
        if not dry_run: sys.exit(f"   -> CRITICAL: Could not fetch existing policies from {get_policies_url}. Exiting.")
        print("   -> WARNING: Could not fetch existing policies. Assuming all policies are new for this dry run.")

    # Iterate through each policy definition in the YAML
    for policy_name, policy_data in policies_config.items():
        print(f"\n   -> Processing policy: '{policy_name}'")

        desired_policy = {
            "name": policy_name,
            "description": policy_data.get("description", f"IaC-managed policy: {policy_name}"),
            "namespace": "default",
            "package_policies": []
        }

        # For each integration, find its definition, then its fragment, and add it
        for def_key in policy_data.get("integrations", []):
            if def_key not in definitions:
                print(f"      ⚠️  Warning: Integration definition '{def_key}' not found in YAML for policy '{policy_name}'. Skipping.")
                continue
            
            fragment_filename = definitions[def_key].get("fragment")
            if not fragment_filename:
                print(f"      ⚠️  Warning: Definition '{def_key}' is missing a 'fragment' key. Skipping.")
                continue

            fragment_path = fragments_dir / f"{fragment_filename}.json"
            if not fragment_path.exists():
                print(f"      ⚠️  Warning: Fragment file '{fragment_path}' not found for definition '{def_key}'. Skipping.")
                continue
            
            with open(fragment_path, 'r') as f:
                fragment_content = json.load(f)
            desired_policy["package_policies"].append(fragment_content)

        # Idempotent Apply Logic
        if policy_name in policy_name_to_id:
            policy_id = policy_name_to_id[policy_name]
            print(f"      -> Plan: UPDATE existing policy (ID: {policy_id}).")
            make_api_request("PUT", f"{kibana_url}/api/fleet/agent_policies/{policy_id}", api_key, json=desired_policy, dry_run=dry_run)
            if not dry_run: print(f"      ✅ Updated.")
        else:
            print(f"      -> Plan: CREATE new policy.")
            make_api_request("POST", f"{kibana_url}/api/fleet/agent_policies", api_key, json=desired_policy, dry_run=dry_run)
            if not dry_run: print(f"      ✅ Created.")

def main() -> None:
    parser = argparse.ArgumentParser(description="Build or update an Elastic deployment from an IaC state definition.")
    parser.add_argument("--url", required=True, help="The base URL of your Kibana instance.")
    parser.add_argument("--es-url", help="Optional: The base URL of your Elasticsearch instance. If not provided, it will be derived from the Kibana URL.")
    parser.add_argument("--state-dir", default="fleet_state_discovered", help="Directory containing the state files.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions instead of executing them.")
    args = parser.parse_args()

    kibana_url = args.url.rstrip("/")
    api_key = get_api_key()
    state_dir = pathlib.Path(args.state_dir)
    definition_file = state_dir / "fleet_definition.yaml"

    if not definition_file.exists():
        sys.exit(f"❌ Definition file not found: {definition_file}")

    if args.dry_run:
        print("="*50)
        print("=== DRY RUN MODE ENABLED: No changes will be made. ===")
        print("="*50)

    print(f"🚀 Starting build process for {kibana_url}")
    print(f"   Using state definition from '{state_dir}/'")

    with open(definition_file, 'r') as f:
        config = yaml.safe_load(f)

    es_url = args.es_url
    if not es_url:
        print("⚠️  --es-url not provided, deriving from Kibana URL. This may not work for self-managed deployments.")
        es_url = kibana_url.replace("kb.", "es.")
    es_url = es_url.rstrip("/")
    
    # Phase 1: Apply foundational assets
    if "foundational_assets" in config:
        apply_foundational_assets(es_url, api_key, state_dir, config.get("foundational_assets", {}), args.dry_run)
    
    # Phase 2: Apply agent policies
    generate_and_apply_agent_policies(kibana_url, api_key, config, state_dir, args.dry_run)

    if args.dry_run:
        print("\n✅ Dry run complete. Review the planned actions above.")
    else:
        print("\n✅ Build process complete!")

if __name__ == "__main__":
    main()

