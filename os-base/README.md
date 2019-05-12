OS Base
=========

A basic OS setup for my current needs.

Role Variables
--------------

* **hostname** - Defines the hostname.
* **open_ports** - Selects the ports that we should open for listening.

Example Playbook
----------------

Including an example of how to use your role (for instance, with variables passed in as parameters)
is always nice for users too:

    - hosts: servers
      roles:
         - { role: kriansa.os-base, hostname: my-cool-server, open_ports: [ "80/tcp" ] }

License
-------

3-Clause BSD.
