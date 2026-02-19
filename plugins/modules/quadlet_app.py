# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

# pylint: disable=import-error
from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-not-found]

# pylint: enable=import-error

DOCUMENTATION = r"""
---
module: quadlet_app
short_description: Deploy Podman Quadlet applications with automatic templating and preprocessing
version_added: "1.0.0"
description:
    - Deploys Podman Quadlet applications with automatic resource prefixing
    - Templates all files with Jinja2 variables (handled by Ansible)
    - Preprocesses Quadlet files to add application name prefixes
    - Manages init.d and config.d directories
    - Handles systemd service lifecycle (daemon-reload, start, enable)
    - Ensures idempotent deployments (only updates when files change)

options:
    src:
        description:
            - Path to the application directory containing quadlets/, init.d/, config.d/
            - Can be relative to playbook directory or absolute path
            - Must contain quadlets/main.container (required)
        required: true
        type: path
    name:
        description:
            - Application name used for prefixing files and services
            - If not provided, extracted from basename of src
            - Must start with a letter (a-z)
            - Can end with a letter or number (a-z, 0-9)
            - Can contain lowercase letters, numbers, hyphens, underscores
            - Automatically converted to lowercase
        required: false
        type: str
    state:
        description:
            - Desired deployment state
            - C(installed) - Deploy files and reload systemd (no service management)
            - C(started) - Deploy files, reload systemd, start main service
            - C(restarted) - Deploy files, reload systemd, restart main service
        required: false
        type: str
        choices: ['installed', 'started', 'restarted']
        default: installed
    force:
        description:
            - Force overwrite files even if content hasn't changed
            - When false, uses checksum comparison for idempotency
        required: false
        type: bool
        default: false
    systemctl_timeout:
        description:
            - Timeout in seconds for systemctl commands (start, stop, restart, daemon-reload)
            - Increase this for services that take a long time to start (e.g., large image pulls)
        required: false
        type: int
        default: 120

author:
    - Daniel Pereira (@kriansa)

requirements:
    - python >= 3.9
    - podman >= 4.4

notes:
    - This module requires root privileges or appropriate systemd user permissions
    - Quadlet files must be in quadlets/ subdirectory
    - At least quadlets/main.container must exist
    - init.d and config.d subdirectories must end with .container or .pod
    - All files are automatically templated by Ansible before module receives them
"""

EXAMPLES = r"""
# Deploy application with default settings
- name: Deploy simple web app
  kriansa.commons.quadlet_app:
    src: podman-apps/nginx
    state: installed

# Deploy and start application with custom name
- name: Deploy backend application
  kriansa.commons.quadlet_app:
    src: podman-apps/backend
    name: prod-backend
    state: started

# Deploy with variables for templating
- name: Deploy database
  kriansa.commons.quadlet_app:
    src: podman-apps/postgres
    state: started
  vars:
    db_version: "16"
    db_password: "{{ vault_db_password }}"

# Force restart application
- name: Update and restart application
  kriansa.commons.quadlet_app:
    src: podman-apps/webapp
    state: restarted
    force: true

# Deploy service with extended timeout for slow image pulls
- name: Deploy application with custom timeout
  kriansa.commons.quadlet_app:
    src: podman-apps/unifi-network
    state: started
    systemctl_timeout: 300
"""

RETURN = r"""
changed:
    description: Whether any changes were made
    type: bool
    returned: always
    sample: true
application_name:
    description: The application name used for deployment
    type: str
    returned: always
    sample: "my-backend-app"
service_name:
    description: The main systemd service name
    type: str
    returned: always
    sample: "my-backend-app--main.service"
quadlet_files:
    description: List of deployed quadlet files
    type: list
    returned: always
    sample: ["my-app--main.container", "my-app--db.container", "my-app--data.volume"]
msg:
    description: Human-readable message about the operation
    type: str
    returned: always
    sample: "quadlet files deployed and service started"
"""

# Default deployment paths
QUADLET_SYSTEMD_DIR = "/etc/containers/systemd"
QUADLET_APP_BASE_DIR = "/srv"


class ValidationError(Exception):
    """Custom exception for validation errors with detailed messages."""


class QuadletValidator:
    """Handles all validation logic for the quadlet_app module."""

    # Regex pattern for application name validation
    APP_NAME_PATTERN_SINGLE = r"^[a-z]$"
    APP_NAME_PATTERN_MULTI = r"^[a-z][a-z0-9_-]*[a-z0-9]$"

    # Valid quadlet file extensions
    VALID_QUADLET_EXTENSIONS = {".container", ".volume", ".network", ".pod", ".kube"}

    # Valid init.d/config.d subdirectory suffixes
    VALID_SUBDIR_SUFFIXES = {".container", ".pod"}

    def __init__(self, src: str, app_name: str):
        """
        Initialize validator.

        Args:
            src: Source directory path
            app_name: Application name (will be normalized)
        """
        self.src = src
        self.app_name = app_name.lower()  # Normalize to lowercase

    def validate_all(self) -> str:
        """
        Run all validation checks.

        Returns:
            Normalized application name

        Raises:
            ValidationError: If any validation check fails
        """
        self.validate_source_directory()
        self.validate_app_name()
        self.validate_main_container()
        self.validate_init_config_structure()
        return self.app_name

    def validate_source_directory(self):
        """Validate source directory exists and is a directory."""
        if not os.path.exists(self.src):
            raise ValidationError(f"Source directory not found: {self.src}")

        if not os.path.isdir(self.src):
            raise ValidationError(f"Source path is not a directory: {self.src}")

        # Check that quadlets directory exists
        quadlets_dir = os.path.join(self.src, "quadlets")
        if not os.path.isdir(quadlets_dir):
            raise ValidationError(
                f"Required directory not found: {quadlets_dir}. "
                f"The quadlets/ subdirectory is mandatory."
            )

    def validate_app_name(self):
        """Validate application name format."""
        # Check pattern based on length
        if len(self.app_name) == 1:
            pattern = self.APP_NAME_PATTERN_SINGLE
        else:
            pattern = self.APP_NAME_PATTERN_MULTI

        if not re.match(pattern, self.app_name):
            raise ValidationError(
                f"Invalid application name: {self.app_name}. "
                "Must start with a letter (a-z), end with a letter or number, "
                "and contain only lowercase letters, numbers, hyphens, and underscores."
            )

    def validate_main_container(self):
        """Ensure quadlets/main.container exists."""
        main_container = os.path.join(self.src, "quadlets", "main.container")
        if not os.path.isfile(main_container):
            raise ValidationError(
                f"Required file not found: {main_container}. "
                "The quadlets/main.container file is mandatory."
            )

    def validate_init_config_structure(self):
        """
        Validate init.d and config.d subdirectory structure.

        Rules:
        1. Subdirectories use the quadlet stem without suffix (e.g., main/, not main.container/)
        2. A corresponding .container or .pod quadlet file must exist
        3. The match must be unambiguous (no both main.container and main.pod)
        """
        for dir_name in ["init.d", "config.d"]:
            dir_path = os.path.join(self.src, dir_name)
            if not os.path.isdir(dir_path):
                continue

            try:
                entries = os.listdir(dir_path)
            except OSError as e:
                raise ValidationError(f"Cannot read {dir_name}: {e}") from e

            for entry in entries:
                entry_path = os.path.join(dir_path, entry)
                if not os.path.isdir(entry_path):
                    continue

                # Reject suffixed directories
                if Path(entry).suffix in self.VALID_SUBDIR_SUFFIXES:
                    raise ValidationError(
                        f"Invalid {dir_name} subdirectory: {entry}. "
                        f"Use the quadlet stem without suffix (e.g., {Path(entry).stem}/ instead of {entry}/)"
                    )

                # Find matching quadlet files by stem
                quadlets_dir = os.path.join(self.src, "quadlets")
                matching = [
                    f for f in os.listdir(quadlets_dir)
                    if Path(f).stem == entry and Path(f).suffix in self.VALID_SUBDIR_SUFFIXES
                ]

                if not matching:
                    raise ValidationError(
                        f"No corresponding quadlet file for {dir_name}/{entry}. "
                        f"Expected a file like quadlets/{entry}.container or quadlets/{entry}.pod"
                    )

                if len(matching) > 1:
                    raise ValidationError(
                        f"Ambiguous {dir_name} subdirectory: {entry}. "
                        f"Multiple matching quadlet files found: {', '.join(sorted(matching))}. "
                        f"Remove one of the conflicting quadlet files"
                    )


class QuadletFileDiscovery:  # pylint: disable=too-few-public-methods
    """Handles discovery of quadlet files and associated resources."""

    def __init__(self, src: str):
        """
        Initialize file discovery.

        Args:
            src: Source directory path
        """
        self.src = src

    def discover_all_files(self) -> Dict[str, List[Tuple[str, str]]]:
        """
        Discover all files that need to be deployed.

        Returns:
            Dictionary with keys:
            - 'quadlets': List of (filename, full_path) for quadlet files
            - 'init': List of (container_dir, relative_path, full_path) for init files
            - 'config': List of (container_dir, relative_path, full_path) for config files
        """
        files: Dict[str, List] = {"quadlets": [], "init": [], "config": []}

        files["quadlets"] = self._discover_quadlet_files()
        files["init"] = self._discover_init_files()
        files["config"] = self._discover_config_files()

        return files

    def _discover_quadlet_files(self) -> List[Tuple[str, str]]:
        """
        Discover all quadlet files in quadlets/ directory.

        Returns:
            List of (filename, full_path) tuples
        """
        quadlets_dir = os.path.join(self.src, "quadlets")
        quadlet_files = []

        valid_extensions = (".container", ".volume", ".network", ".pod", ".kube")

        try:
            entries = os.listdir(quadlets_dir)
        except OSError as e:
            raise ValidationError(f"Cannot read quadlets directory: {e}") from e

        for filename in sorted(entries):  # Sort for consistent ordering
            if filename.endswith(valid_extensions):
                full_path = os.path.join(quadlets_dir, filename)
                if os.path.isfile(full_path):
                    quadlet_files.append((filename, full_path))

        return quadlet_files

    def _discover_init_files(self) -> List[Tuple[str, str, str]]:
        """
        Discover all init files in init.d/ directory.

        Returns:
            List of (container_dir, relative_path, full_path) tuples
        """
        init_dir = os.path.join(self.src, "init.d")
        if not os.path.isdir(init_dir):
            return []

        init_files = []

        # Walk through container subdirectories
        for container_dir in sorted(os.listdir(init_dir)):
            container_path = os.path.join(init_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            # Walk through all files in this container directory
            for root, _, files in os.walk(container_path):
                # Sort for consistent ordering
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    # Get path relative to container directory
                    rel_path = os.path.relpath(full_path, container_path)
                    init_files.append((container_dir, rel_path, full_path))

        return init_files

    def _discover_config_files(self) -> List[Tuple[str, str, str]]:
        """
        Discover all config files in config.d/ directory.

        Returns:
            List of (container_dir, relative_path, full_path) tuples
        """
        config_dir = os.path.join(self.src, "config.d")
        if not os.path.isdir(config_dir):
            return []

        config_files = []

        # Walk through container subdirectories
        for container_dir in sorted(os.listdir(config_dir)):
            container_path = os.path.join(config_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            # Walk through all files in this container directory
            for root, _, files in os.walk(container_path):
                # Sort for consistent ordering
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    # Get path relative to container directory
                    rel_path = os.path.relpath(full_path, container_path)
                    config_files.append((container_dir, rel_path, full_path))

        return config_files


class QuadletPreprocessor:  # pylint: disable=too-few-public-methods
    """
    Handles preprocessing of quadlet files according to specification rules.

    Rule 1: Prefix resource references (Network=, Pod=, Volume=)
    Rule 2: Replace init.d/config.d volume paths
    """

    # File extensions that need Rule 1 (prefix resources)
    RULE1_EXTENSIONS = {".container", ".pod", ".kube"}

    # File extensions that need Rule 2 (replace paths)
    RULE2_EXTENSIONS = {".container", ".pod"}

    # Resource name directives for Rule 3 (inject names)
    RESOURCE_NAME_DIRECTIVES = {
        ".container": ("Container", "ContainerName"),
        ".pod": ("Pod", "PodName"),
        ".volume": ("Volume", "VolumeName"),
        ".network": ("Network", "NetworkName"),
    }

    # Directives to process for Rule 1
    RESOURCE_DIRECTIVES = {
        "Network",
        "Pod",
        "Volume",
        # Systemd unit dependencies
        "Wants",
        "Requires",
        "Requisite",
        "BindsTo",
        "PartOf",
        "Upholds",
        "Conflicts",
        "Before",
        "After",
    }

    def __init__(self, app_name: str):
        """
        Initialize preprocessor.

        Args:
            app_name: Application name to use for prefixing
        """
        self.app_name = app_name

    def preprocess_quadlet_file(self, content: str, filename: str) -> str:
        """
        Apply preprocessing rules to quadlet file content.

        Args:
            content: File content (already templated by Ansible)
            filename: Quadlet filename (e.g., "main.container")

        Returns:
            Preprocessed content
        """
        file_ext = Path(filename).suffix

        # Apply Rule 3 first (inject resource names) - operates on full content
        content = self._apply_rule3_inject_names(content, filename)

        # Determine which rules apply
        needs_rule1 = file_ext in self.RULE1_EXTENSIONS
        needs_rule2 = file_ext in self.RULE2_EXTENSIONS

        # Extract container/pod name from filename (without extension)
        container_name = Path(filename).stem

        # Process line by line (Rules 1 and 2)
        lines = content.split("\n")
        processed_lines = []

        for line in lines:
            # Skip empty lines and comments
            if not line.strip() or line.strip().startswith("#"):
                processed_lines.append(line)
                continue

            # Apply preprocessing rules
            if needs_rule1:
                line = self._apply_rule1_prefix_resources(line)

            if needs_rule2:
                line = self._apply_rule2_replace_paths(line, container_name)

            processed_lines.append(line)

        return "\n".join(processed_lines)

    def _apply_rule1_prefix_resources(self, line: str) -> str:
        """
        Rule 1: Add application name prefix to resource references.

        Transforms:
            Network=main.network -> Network=myapp--main.network
            Pod=backend.pod -> Pod=myapp--backend.pod
            Volume=data.volume:... -> Volume=myapp--data.volume:...
            Wants=db.service -> Wants=myapp--db.service
            After=main.service -> After=myapp--main.service

        Args:
            line: Single line from quadlet file

        Returns:
            Processed line with prefixed resources
        """
        # Check if line starts with a resource directive
        line_stripped = line.lstrip()
        if not line_stripped:
            return line

        # Extract directive and value
        if "=" not in line_stripped:
            return line

        directive, _, value = line_stripped.partition("=")

        # Only process resource directives
        if directive not in self.RESOURCE_DIRECTIVES:
            return line

        # Preserve leading whitespace
        leading_ws = line[: len(line) - len(line_stripped)]

        # Parse the value part
        # For Volume=, the format is: source:dest:options
        # We only prefix if source matches pattern *.{network,pod,volume}
        if directive == "Volume":
            # Check if this looks like a quadlet volume reference
            # Pattern: starts with non-slash, contains .volume before : or end
            volume_pattern = r"^([^/][^:]*\.(volume|network|pod))(:.+)?$"
            match = re.match(volume_pattern, value)
            if match:
                resource_ref = match.group(1)
                rest = match.group(3) or ""
                prefixed = f"{self.app_name}--{resource_ref}"
                return f"{leading_ws}{directive}={prefixed}{rest}"
        else:
            # For other directives, check if value ends with a quadlet-related extension
            quadlet_extensions = (
                ".network",
                ".pod",
                ".volume",
                ".container",
                ".kube",
                ".service",
            )
            if value.endswith(quadlet_extensions):
                # Only prefix if it doesn't start with / (not absolute path)
                if not value.startswith("/"):
                    prefixed = f"{self.app_name}--{value}"
                    return f"{leading_ws}{directive}={prefixed}"

        return line

    def _apply_rule2_replace_paths(self, line: str, container_name: str) -> str:
        """
        Rule 2: Replace init.d/config.d volume paths.

        Transforms:
            Volume=init.d:... -> Volume={QUADLET_APP_BASE_DIR}/myapp/init/container:...
            Volume=config.d/sub:... -> Volume={QUADLET_APP_BASE_DIR}/myapp/config/container/sub:...

        Args:
            line: Single line from quadlet file
            container_name: Name of the container (without extension)

        Returns:
            Processed line with replaced paths
        """
        # Only process Volume= directives
        line_stripped = line.lstrip()
        if not line_stripped.startswith("Volume="):
            return line

        # Preserve leading whitespace
        leading_ws = line[: len(line) - len(line_stripped)]

        # Extract value part (after Volume=)
        value = line_stripped[7:]  # Remove "Volume="

        # Parse Volume directive: Volume=SOURCE:DEST:OPTIONS
        parts = value.split(":", 1)
        if len(parts) < 1:
            return line

        source = parts[0]
        rest = ":" + parts[1] if len(parts) > 1 else ""

        # Skip if source ends with .volume (already handled by Rule 1)
        if source.endswith(".volume"):
            return line

        # Skip if source is an absolute path
        if source.startswith("/"):
            return line

        # Process init.d paths
        if source.startswith("init.d"):
            subpath = source[6:]  # Remove 'init.d' prefix
            # Remove leading slash if present
            if subpath.startswith("/"):
                subpath = subpath[1:]

            # Build replacement path
            new_source = f"{QUADLET_APP_BASE_DIR}/{self.app_name}/init/{container_name}"
            if subpath:
                new_source += f"/{subpath}"

            return f"{leading_ws}Volume={new_source}{rest}"

        # Process config.d paths
        if source.startswith("config.d"):
            subpath = source[8:]  # Remove 'config.d' prefix
            # Remove leading slash if present
            if subpath.startswith("/"):
                subpath = subpath[1:]

            # Build replacement path
            new_source = f"{QUADLET_APP_BASE_DIR}/{self.app_name}/config/{container_name}"
            if subpath:
                new_source += f"/{subpath}"

            return f"{leading_ws}Volume={new_source}{rest}"

        return line

    def _apply_rule3_inject_names(self, content: str, filename: str) -> str:
        """
        Rule 3: Inject resource name directives if missing.

        Adds ContainerName, PodName, VolumeName, or NetworkName to the
        appropriate section if not already present.

        Args:
            content: File content
            filename: Quadlet filename (e.g., "main.container")

        Returns:
            Content with name directive injected if needed
        """
        file_ext = Path(filename).suffix

        # Check if this file type needs name injection
        if file_ext not in self.RESOURCE_NAME_DIRECTIVES:
            return content

        section_name, directive_name = self.RESOURCE_NAME_DIRECTIVES[file_ext]
        resource_name = Path(filename).stem
        name_value = f"{self.app_name}--{resource_name}"

        # Parse content into sections
        sections = []
        current_section = None
        current_lines = []

        for line in content.split("\n"):
            stripped = line.strip()
            # Check if this is a section header
            if stripped.startswith("[") and stripped.endswith("]"):
                # Save previous section
                if current_section is not None:
                    sections.append((current_section, current_lines))
                # Start new section
                current_section = stripped[1:-1]  # Remove brackets
                current_lines = [line]
            else:
                current_lines.append(line)

        # Save last section
        if current_section is not None:
            sections.append((current_section, current_lines))

        # Find target section and inject directive if needed
        for i, (section, lines) in enumerate(sections):
            if section == section_name:
                # Check if directive already exists (ignore comments)
                has_directive = any(
                    ln.strip().startswith(f"{directive_name}=")
                    for ln in lines
                    if ln.strip() and not ln.strip().startswith("#")
                )

                if not has_directive:
                    # Find section header and inject after it
                    for j, line in enumerate(lines):
                        if line.strip().startswith("["):
                            lines.insert(j + 1, f"{directive_name}={name_value}")
                            break

                sections[i] = (section, lines)
                break

        # Reconstruct content
        all_lines = []
        for _section_name, lines in sections:
            all_lines.extend(lines)

        return "\n".join(all_lines)


class QuadletIdempotency:  # pylint: disable=too-few-public-methods
    """Handles idempotency checking using file checksums."""

    def __init__(self, app_name: str, force: bool):
        """
        Initialize idempotency checker.

        Args:
            app_name: Application name
            force: If True, always report changes needed
        """
        self.app_name = app_name
        self.force = force
        self.quadlet_dest = QUADLET_SYSTEMD_DIR
        self.srv_base = f"{QUADLET_APP_BASE_DIR}/{app_name}"

    def needs_deployment(self, processed_files: Dict[str, str]) -> bool:
        """
        Check if deployment is needed based on file changes.

        Args:
            processed_files: Dict mapping destination_path -> content

        Returns:
            True if deployment is needed, False otherwise
        """
        if self.force:
            return True

        for dest_path, new_content in processed_files.items():
            if self._file_changed(dest_path, new_content):
                return True

        return False

    def _file_changed(self, dest_path: str, new_content: str) -> bool:
        """
        Check if a file has changed by comparing checksums.

        Args:
            dest_path: Destination file path
            new_content: New file content

        Returns:
            True if file doesn't exist or content differs
        """
        # Check if file exists
        if not os.path.exists(dest_path):
            return True

        # Check if it's a regular file
        if not os.path.isfile(dest_path):
            return True

        # Read existing content
        try:
            with open(dest_path, "r", encoding="utf-8") as f:
                existing_content = f.read()
        except (IOError, OSError, UnicodeDecodeError):
            # If we can't read it, assume it changed
            return True

        # Compare checksums
        existing_checksum = self._calculate_checksum(existing_content)
        new_checksum = self._calculate_checksum(new_content)

        return existing_checksum != new_checksum

    @staticmethod
    def _calculate_checksum(content: str) -> str:
        """
        Calculate SHA256 checksum of content.

        Args:
            content: String content

        Returns:
            Hex digest of SHA256 hash
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class QuadletAppModule:  # pylint: disable=too-few-public-methods
    """Main module class coordinating all components."""

    def __init__(self, module: AnsibleModule):
        """
        Initialize the module.

        Args:
            module: AnsibleModule instance
        """
        self.module = module
        self.params = module.params
        self.changed = False

        # Extract parameters
        self.src = self.params["src"]
        self.app_name = self.params.get("name") or os.path.basename(self.src)
        self.state = self.params["state"]
        self.force = self.params["force"]
        self.systemctl_timeout = self.params["systemctl_timeout"]

        # Check if files were processed by action plugin on control node
        self.control_node_processed = self.params.get("_control_node_processed", False)
        self.files_data = self.params.get("_files_data", None)

        # Paths
        self.quadlet_dest = QUADLET_SYSTEMD_DIR
        self.srv_base: str | None = None  # Set after app_name validation

        # Initialize components
        self.validator: QuadletValidator | None = None
        self.discovery: QuadletFileDiscovery | None = None
        self.preprocessor: QuadletPreprocessor | None = None
        self.idempotency: QuadletIdempotency | None = None

    def run(self):
        """Main execution flow."""
        try:
            # Phase 1: Validation (app name only if from action plugin)
            if self.control_node_processed:
                # Minimal validation - action plugin already validated structure
                self.app_name = self.app_name.lower()
                # Validate app name format
                if len(self.app_name) == 1:
                    pattern = r"^[a-z]$"
                else:
                    pattern = r"^[a-z][a-z0-9_-]*[a-z0-9]$"
                if not re.match(pattern, self.app_name):
                    raise ValidationError(
                        f"Invalid application name: {self.app_name}. "
                        "Must start with a letter (a-z), end with a letter or number, "
                        "and contain only lowercase letters, numbers, hyphens, and underscores."
                    )
            else:
                # Full validation when running without action plugin
                self.validator = QuadletValidator(self.src, self.app_name)
                self.app_name = self.validator.validate_all()

            self.srv_base = f"{QUADLET_APP_BASE_DIR}/{self.app_name}"

            # Initialize remaining components after app_name validation
            self.preprocessor = QuadletPreprocessor(self.app_name)
            self.idempotency = QuadletIdempotency(self.app_name, self.force)

            # Phase 2 & 3: Get file content (either from action plugin or local)
            if self.control_node_processed:
                # Files already templated by action plugin
                processed_files = self._process_files_from_action_plugin()
                quadlet_names = [f["name"] for f in self.files_data["quadlets"]]
            else:
                # Discover and read files locally (fallback mode)
                self.discovery = QuadletFileDiscovery(self.src)
                discovered_files = self.discovery.discover_all_files()
                processed_files = self._process_files(discovered_files)
                quadlet_names = [name for name, _ in discovered_files["quadlets"]]

            # Phase 4: Check idempotency
            needs_update = self.idempotency.needs_deployment(processed_files)

            # Also check if service state needs management
            service_name = f"{self.app_name}--main.service"
            needs_service_management = self._needs_service_management(service_name)

            if not needs_update and not needs_service_management:
                # No changes needed - files and service state are correct
                self.module.exit_json(
                    changed=False,
                    application_name=self.app_name,
                    service_name=service_name,
                    quadlet_files=[f"{self.app_name}--{name}" for name in quadlet_names],
                    msg="application already up to date",
                )

            # Phase 5: Deploy files (only if they changed)
            if needs_update:
                self._deploy_files(processed_files)
                self.changed = True

            # Phase 6: Manage systemd (if files changed OR service needs management)
            if needs_update or needs_service_management:
                service_changed = self._manage_systemd()
                if service_changed:
                    self.changed = True

            # Success
            self.module.exit_json(
                changed=self.changed,
                application_name=self.app_name,
                service_name=service_name,
                quadlet_files=[f"{self.app_name}--{name}" for name in quadlet_names],
                msg=self._get_success_message(),
            )

        except ValidationError as e:
            self.module.fail_json(msg=str(e))
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.module.fail_json(msg=f"Unexpected error: {str(e)}")

    def _process_files(self, discovered_files: Dict) -> Dict[str, str]:
        """
        Process all discovered files.

        Args:
            discovered_files: Output from FileDiscovery.discover_all_files()

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.preprocessor is not None
        assert self.srv_base is not None

        processed = {}

        # Process quadlet files (need preprocessing)
        for filename, full_path in discovered_files["quadlets"]:
            content = self._read_file(full_path)
            processed_content = self.preprocessor.preprocess_quadlet_file(content, filename)

            dest_path = os.path.join(self.quadlet_dest, f"{self.app_name}--{filename}")
            processed[dest_path] = processed_content

        # Process init files (no preprocessing)
        for container_dir, rel_path, full_path in discovered_files["init"]:
            content = self._read_file(full_path)
            container_name = Path(container_dir).stem  # Remove .container extension

            dest_path = os.path.join(self.srv_base, "init", container_name, rel_path)
            processed[dest_path] = content

        # Process config files (no preprocessing)
        for container_dir, rel_path, full_path in discovered_files["config"]:
            content = self._read_file(full_path)
            container_name = Path(container_dir).stem  # Remove .container extension

            dest_path = os.path.join(self.srv_base, "config", container_name, rel_path)
            processed[dest_path] = content

        return processed

    def _process_files_from_action_plugin(self) -> Dict[str, str]:
        """
        Process files that were already templated by the action plugin.

        Files are already templated - we only need to preprocess quadlets
        and map to destination paths.

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.preprocessor is not None
        assert self.srv_base is not None

        processed = {}

        # Process quadlet files (need preprocessing)
        for file_data in self.files_data["quadlets"]:
            filename = file_data["name"]
            content = file_data["content"]

            # Apply preprocessing rules
            processed_content = self.preprocessor.preprocess_quadlet_file(content, filename)

            dest_path = os.path.join(self.quadlet_dest, f"{self.app_name}--{filename}")
            processed[dest_path] = processed_content

        # Process init files (no preprocessing, already templated)
        for file_data in self.files_data["init"]:
            container_dir = file_data["container"]
            rel_path = file_data["path"]
            content = file_data["content"]

            container_name = Path(container_dir).stem  # Remove .container extension

            dest_path = os.path.join(self.srv_base, "init", container_name, rel_path)
            processed[dest_path] = content

        # Process config files (no preprocessing, already templated)
        for file_data in self.files_data["config"]:
            container_dir = file_data["container"]
            rel_path = file_data["path"]
            content = file_data["content"]

            container_name = Path(container_dir).stem  # Remove .container extension

            dest_path = os.path.join(self.srv_base, "config", container_name, rel_path)
            processed[dest_path] = content

        return processed

    def _deploy_files(self, processed_files: Dict[str, str]):
        """
        Deploy all processed files to their destinations.

        Args:
            processed_files: Dict mapping destination_path -> content
        """
        for dest_path, content in processed_files.items():
            # Create parent directory
            dest_dir = os.path.dirname(dest_path)
            try:
                os.makedirs(dest_dir, mode=0o755, exist_ok=True)
            except OSError as e:
                self.module.fail_json(msg=f"Failed to create directory {dest_dir}: {e}")

            # Write file
            try:
                with open(dest_path, "w", encoding="utf-8") as f:
                    f.write(content)
                # Set appropriate permissions
                os.chmod(dest_path, 0o644)
            except (IOError, OSError) as e:
                self.module.fail_json(msg=f"Failed to write file {dest_path}: {e}")

    def _manage_systemd(self):
        """
        Manage systemd: reload, enable, start/restart.

        Returns:
            bool: True if any systemd operations were performed
        """
        service_changed = False
        service_name = f"{self.app_name}--main.service"

        # Reload daemon if files changed
        if self.changed:
            self._validate_quadlet_syntax()
            self._systemctl(["daemon-reload"])
            service_changed = True

        # Determine if we need to restart dependencies and main service
        needs_restart = False
        if self.state == "restarted":
            # Always restart when restarted state is requested
            needs_restart = True
        elif self.state == "started" and self.changed and self._is_service_active(service_name):
            # Restart if files changed and service is already running
            needs_restart = True

        # Restart dependencies and main service if needed
        if needs_restart:
            # Restart all app-prefixed dependencies first
            self._restart_dependencies(service_name)
            # Then restart the main service
            self._systemctl(["restart", service_name])
            service_changed = True
        elif self.state == "started" and not self._is_service_active(service_name):
            # Start service if not already active (no restart needed)
            self._systemctl(["start", service_name])
            service_changed = True

        return service_changed

    def _get_app_dependencies(self, service_name: str) -> List[str]:
        """
        Get list of app-prefixed service dependencies.

        Args:
            service_name: Main service name to query dependencies for

        Returns:
            List of app-prefixed dependency service names
        """
        try:
            # List all service dependencies
            result = subprocess.run(
                ["systemctl", "list-dependencies", "--type", "service", service_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            if result.returncode != 0:
                # If service doesn't exist yet or has no dependencies, return empty list
                return []

            # Parse output and filter for app-prefixed services
            dependencies = []
            app_prefix = f"{self.app_name}--"

            for line in result.stdout.splitlines():
                # Remove leading whitespace (--type only adds spaces, no tree chars)
                line_stripped = line.lstrip()

                # Check if this is an app-prefixed service (but not the main service itself)
                if line_stripped.startswith(app_prefix) and line_stripped != service_name:
                    dependencies.append(line_stripped)

            return dependencies

        except subprocess.TimeoutExpired:
            # If listing dependencies times out, return empty list
            return []
        except FileNotFoundError as e:
            # systemctl not found - this is a fatal error
            self.module.fail_json(msg=f"systemctl command not found: {e}")
            return []  # unreachable, but for type checking

    def _restart_dependencies(self, service_name: str):
        """
        Restart all app-prefixed dependencies of the service.

        Args:
            service_name: Main service name to find dependencies for

        Raises:
            Fails the module if any dependency restart fails
        """
        dependencies = self._get_app_dependencies(service_name)

        if not dependencies:
            # No dependencies to restart
            return

        # Restart all dependencies
        # We restart them all together for efficiency
        self._systemctl(["restart"] + dependencies)

    def _validate_quadlet_syntax(self):
        """Validate quadlet files syntax using podman-system-generator dry run."""
        generator = "/usr/lib/systemd/system-generators/podman-system-generator"
        cmd = [generator, "-dryrun", "-v"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            self.module.fail_json(msg=f"{generator} -dryrun -v timed out")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.module.fail_json(msg=f"Failed to execute quadlet syntax validation: {e}")

        if result.returncode != 0:
            self.module.fail_json(
                msg=result.stderr.strip(),
                cmd=" ".join(cmd),
                rc=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    def _systemctl(self, args: List[str]):
        """
        Execute systemctl command.

        Args:
            args: Command arguments (e.g., ['daemon-reload'])
        """
        cmd = ["systemctl"] + args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=self.systemctl_timeout)
            if result.returncode != 0:
                self.module.fail_json(
                    msg=f"systemctl {' '.join(args)} failed: {result.stderr.strip()}",
                    cmd=" ".join(cmd),
                    rc=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        except subprocess.TimeoutExpired:
            self.module.fail_json(msg=f"systemctl {' '.join(args)} timed out after {self.systemctl_timeout}s")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.module.fail_json(msg=f"Failed to execute systemctl: {e}")

    def _needs_service_management(self, service_name: str) -> bool:
        """
        Check if service needs management based on desired state.

        Args:
            service_name: Service name to check

        Returns:
            True if service state doesn't match desired state
        """
        if self.state == "started":
            # Need to start if not currently active
            return not self._is_service_active(service_name)
        if self.state == "restarted":
            # Always restart when restarted state is requested
            return True

        return False

    def _is_service_active(self, service_name: str) -> bool:
        """
        Check if service is currently active.

        Args:
            service_name: Service name to check

        Returns:
            True if service is active
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() == "active"
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def _read_file(self, path: str) -> str:
        """
        Read file content.

        Args:
            path: File path

        Returns:
            File content as string
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (IOError, OSError) as e:
            raise ValidationError(f"Failed to read file {path}: {e}") from e
        except UnicodeDecodeError as e:
            raise ValidationError(f"File {path} is not valid UTF-8: {e}") from e

    def _get_quadlet_filenames(self, discovered_files: Dict) -> List[str]:
        """Get list of deployed quadlet filenames with app prefix."""
        return [f"{self.app_name}--{filename}" for filename, _ in discovered_files["quadlets"]]

    def _get_success_message(self) -> str:
        """Get success message based on state."""
        if self.state == "installed":
            return "quadlet files deployed"
        if self.state == "started":
            return "quadlet files deployed and service started"
        if self.state == "restarted":
            return "quadlet files deployed and service restarted"
        return "quadlet files deployed"


def main():
    """Ansible module entry point."""
    module = AnsibleModule(
        argument_spec={
            "src": {"type": "path", "required": True},
            "name": {"type": "str", "required": False},
            "state": {
                "type": "str",
                "default": "installed",
                "choices": ["installed", "started", "restarted"],
            },
            "force": {"type": "bool", "default": False},
            "systemctl_timeout": {"type": "int", "default": 120},
            # Internal parameters used by action plugin
            "_control_node_processed": {"type": "bool", "default": False, "required": False},
            "_files_data": {"type": "dict", "required": False},
        },
        supports_check_mode=False,
    )

    # Run the module
    app_module = QuadletAppModule(module)
    app_module.run()


if __name__ == "__main__":
    main()
