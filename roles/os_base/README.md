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

Troubleshooting
---------------

When using this to bootstrap a very slow machine, you might end up with yum not being able to
install packages due to lock timeout. To solve that, add this to your main playbook:

```yml
- hosts: localhost
  module_defaults:
    yum:
      lock_timeout: 300
```

License
-------

3-Clause BSD.
