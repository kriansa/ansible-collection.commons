OS Base
=========

A basic OS setup for my current RHEL/CentOS/Amazon Linux baseline.

Role Variables
--------------

* **hostname** - Defines the hostname.
* **open_ports** - Selects the ports that we should open for listening.
* **install_cloudwatch** - Whether to install AWS CloudWatch Agent. (default=True)

Example Playbook
----------------

    - hosts: servers
      roles:
         - { role: kriansa/os-base, hostname: my-cool-server, open_ports: [ "80/tcp" ] }

Roadmap
-------

### Add support for CentOS 8.

There are some blockers/things I need to fix before that happen.

1. There's no built in way to get whether a system needs rebooting after an upgrade.
   See: https://www.centos.org/forums/viewtopic.php?t=72109
2. Yum has been superseded by dnf.
3. Need to replace `yum-plugin-priorities` by DNF native support to priorities. Maybe this isn't
   required at all.
4. I need this package to have compatibility between CentOS and Amazon Linux. Currently, some of the
   changes to support CentOS 8 will require lots of conditions.

License
-------

3-Clause BSD.
