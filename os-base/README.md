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

License
-------

3-Clause BSD.
