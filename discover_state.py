#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional

import requests
import yaml

# --- API Helper Functions ---
def get_api_key() -> str:
    """Gets the API key from an environment variable and exits if not found."""
    api_key = os.getenv("ELASTIC_API_KEY")
    if not api_key:
        sys.exit("❌ ELASTIC_API_KEY environment variable is not set.")
    return api_key

def api_get(url: str, api_key: str, params: Optional[Dict] = None) -> Dict:
    """Makes a GET request and handles errors."""
    headers = {"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "true"}
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as exc:
        sys.exit(f"❌ API error calling {url}: {exc}")

# --- Asset Dumping Functions ---
def dump_all_component_templates(es_url: str, api_key: str, out_dir: pathlib.Path) -> List[str]:
    """Fetches and saves all non-managed component templates."""
    print("📂 Dumping component templates...")
    comp_root = out_dir / "component_templates"
    comp_root.mkdir(parents=True, exist_ok=True)
    saved_files = []
    try:
        data = api_get(f"{es_url}/_component_template", api_key)
        templates = data.get("component_templates", [])
        for tpl in templates:
            tpl_name = tpl.get("name", "unnamed")
            # Skip managed templates
            if tpl.get("component_template", {}).get("_meta", {}).get("managed"):
                continue
            output_path = comp_root / f"{tpl_name}.json"
            with open(output_path, "w") as f:
                json.dump(tpl["component_template"], f, indent=2)
            saved_files.append(tpl_name)
        print(f"   -> Saved {len(saved_files)} non-managed component templates.")
        return saved_files
    except Exception as e:
        print(f"   -> ❌ Error querying component templates: {e}")
        return []

def dump_all_ingest_pipelines(es_url: str, api_key: str, out_dir: pathlib.Path) -> List[str]:
    """Fetches and saves all non-managed ingest pipelines."""
    print("📦 Dumping ingest pipelines...")
    pipelines_dir = out_dir / "pipelines"
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    saved_files = []
    try:
        data = api_get(f"{es_url}/_ingest/pipeline", api_key)
        for name, pipeline in data.items():
            # Skip managed pipelines
            if pipeline.get("_meta", {}).get("managed"):
                continue
            with open(pipelines_dir / f"{name}.json", "w") as f:
                json.dump(pipeline, f, indent=2)
            saved_files.append(name)
        print(f"   -> Saved {len(saved_files)} non-managed ingest pipelines.")
        return saved_files
    except Exception as e:
        print(f"   -> ❌ Error querying ingest pipelines: {e}")
        return []

def extract_and_save_integration_fragments(base_url: str, api_key: str, out_dir: pathlib.Path) -> Dict[str, Dict]:
    """
    Fetches policies, saves clean integration fragments, and returns a map of
    policy_id -> list of fragment file names.
    """
    print("🧩 Processing policies and integrations into fragments...")
    policies_data = api_get(f"{base_url}/api/fleet/agent_policies", api_key, {"perPage": 1000, "full": "true"})
    
    policy_to_fragments_map: Dict[str, Dict] = {}
    seen_hashes: Dict[str, str] = {}
    fname_counters = defaultdict(int)
    frag_dir = out_dir / "integration_fragments"
    frag_dir.mkdir(exist_ok=True)
    
    if not policies_data: return {}

    for policy in policies_data.get("items", []):
        policy_id = policy["id"]
        policy_to_fragments_map[policy_id] = {"name": policy["name"], "description": policy["description"], "fragments": []}

        for pkg in policy.get("package_policies", []):
            base_name = pkg.get('name')
            if not base_name: continue
            
            clean_fragment = {key: pkg.get(key) for key in ['name', 'version', 'policy_template', 'vars'] if key in pkg}
            if 'vars' not in clean_fragment: clean_fragment['vars'] = {}
            
            h = hashlib.sha256(json.dumps(clean_fragment, sort_keys=True).encode()).hexdigest()
            if h in seen_hashes:
                fragment_filename = seen_hashes[h]
            else:
                descriptive_name = base_name
                # Make custom_logs fragment names more unique
                if base_name == "custom_logs" and "id" in clean_fragment.get("vars", {}):
                    custom_id = clean_fragment["vars"]["id"].replace('.', '_')
                    descriptive_name = f"{base_name}-{custom_id}"

                fname_counters[descriptive_name] += 1
                fragment_filename = f"{descriptive_name}-{fname_counters[descriptive_name]}" if fname_counters[descriptive_name] > 1 else descriptive_name
                
                with open(frag_dir / f"{fragment_filename}.json", "w") as f:
                    json.dump(clean_fragment, f, indent=2)
                
                seen_hashes[h] = fragment_filename
            
            policy_to_fragments_map[policy_id]["fragments"].append(fragment_filename)
    
    print(f"   -> Created {len(seen_hashes)} unique integration fragments.")
    return policy_to_fragments_map

def build_integration_definitions(fragments_dir: pathlib.Path) -> Dict:
    """Parses fragments to build the integration_definitions block."""
    print("🔗 Analyzing fragments for pipeline dependencies...")
    definitions = {}
    for frag_file in fragments_dir.glob("*.json"):
        with open(frag_file, 'r') as f:
            fragment_data = json.load(f)
        
        definition_key = frag_file.stem
        # Make the key more readable, e.g., 'custom_logs-syslog_aci-1' -> 'syslog_aci'
        clean_key = re.sub(r'^custom_logs-', '', definition_key)
        clean_key = re.sub(r'-[0-9]+$', '', clean_key)

        definitions[clean_key] = {"fragment": frag_file.stem}
        
        pipeline = fragment_data.get("vars", {}).get("pipeline")
        if pipeline:
            definitions[clean_key]["dependencies"] = {"ingest_pipelines": [pipeline]}
    
    print(f"   -> Created {len(definitions)} integration definitions.")
    return definitions

def build_agent_policies_from_state(agents: List[Dict], policy_map: Dict, definitions: Dict) -> Dict:
    """Analyzes policy state to generate the agent_policies block."""
    print("🧠 Analyzing state to build agent policies...")
    fragment_to_definition_key = {v['fragment']: k for k, v in definitions.items()}

    # Group policies by their unique set of integration definitions
    signature_to_policy = {}
    for policy_id, policy_info in policy_map.items():
        if not policy_info["fragments"]: continue

        definition_keys = sorted([fragment_to_definition_key.get(f, f) for f in policy_info["fragments"]])
        signature = hashlib.sha256(" ".join(definition_keys).encode()).hexdigest()

        if signature not in signature_to_policy:
            signature_to_policy[signature] = {
                "policy_name": policy_info["name"],
                "policy_description": policy_info["description"],
                "integrations": definition_keys,
                "agents": [],
            }
    
    policy_id_to_signature = {
        pid: hashlib.sha256(" ".join(sorted([fragment_to_definition_key.get(f, f) for f in p_info["fragments"]])).encode()).hexdigest()
        for pid, p_info in policy_map.items() if p_info["fragments"]
    }

    for agent in agents:
        hostname = agent.get("local_metadata", {}).get("host", {}).get("hostname", agent.get("id"))
        signature = policy_id_to_signature.get(agent.get("policy_id"))
        if signature and signature in signature_to_policy:
            signature_to_policy[signature]["agents"].append(hostname)

    final_policies = {}
    for sig_data in signature_to_policy.values():
        policy_name = sig_data["policy_name"]
        final_policies[policy_name] = {
            "description": sig_data["policy_description"],
            "integrations": sig_data["integrations"],
        }
        if sig_data["agents"]:
            final_policies[policy_name]["_discovered_agents"] = sorted(list(set(sig_data["agents"])))

    print(f"   -> Generated {len(final_policies)} agent policy definitions.")
    return final_policies


def fetch_agents(base_url, api_key):
    print("🕵️  Fetching enrolled agent list...")
    agents_resp = api_get(f"{base_url}/api/fleet/agents", api_key, params={"perPage": 5000})
    agents = agents_resp.get("items", [])
    print(f"   -> Found {len(agents)} agents.")
    return agents


def generate_yaml(output_path: pathlib.Path, templates: List[str], pipelines: List[str], definitions: Dict, policies: Dict):
    """Generates the final fleet_definition.yaml file."""
    print("📝 Generating fleet_definition.yaml...")
    
    final_structure = {
        "foundational_assets": {
            "component_templates": sorted(list(set(templates))),
            "ingest_pipelines": sorted(list(set(pipelines))),
        },
        "integration_definitions": dict(sorted(definitions.items())),
        "agent_policies": dict(sorted(policies.items()))
    }
    
    with open(output_path, 'w') as f:
        yaml.dump(final_structure, f, sort_keys=False, indent=2, default_flow_style=False)
    
    print(f"   -> Successfully wrote state to {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Discover and dump the state of an Elastic Fleet deployment into an IaC structure.")
    parser.add_argument("--url", required=True, help="The base URL of your Kibana instance.")
    parser.add_argument("--es-url", help="Optional: The base URL of your Elasticsearch instance. If not provided, it will be derived from the Kibana URL.")
    parser.add_argument("--output-dir", default="fleet_state_discovered", help="Directory to save the state files.")
    args = parser.parse_args()

    kibana_url = args.url.rstrip("/")
    api_key = get_api_key()
    out_dir = pathlib.Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)
    
    es_url = args.es_url
    if not es_url:
        print("⚠️  --es-url not provided, deriving from Kibana URL. This may not work for self-managed deployments.")
        es_url = kibana_url.replace("kb.", "es.")
    es_url = es_url.rstrip("/")
    
    print(f"🚀 Starting state discovery for {kibana_url}")
    
    templates = dump_all_component_templates(es_url, api_key, out_dir)
    pipelines = dump_all_ingest_pipelines(es_url, api_key, out_dir)
    policy_map = extract_and_save_integration_fragments(kibana_url, api_key, out_dir)
    
    definitions = build_integration_definitions(out_dir / "integration_fragments")
    agents = fetch_agents(kibana_url, api_key)
    policies = build_agent_policies_from_state(agents, policy_map, definitions)
    
    generate_yaml(out_dir / "fleet_definition.yaml", templates, pipelines, definitions, policies)
    
    print("\n✅ Discovery complete!")

if __name__ == "__main__":
    main()

