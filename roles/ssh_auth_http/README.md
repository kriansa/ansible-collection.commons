SSH Auth HTTP
=============

This configures a user to authenticate through SSH by fetching the keys automatically from an HTTP
endpoint, such as Github.

Role Variables
--------------

* **username** - The username to configure
* **authorized_keys_url** - The location of the authorized_keys content

Example Playbook
----------------

  - hosts: servers
    roles:
       - { role: kriansa/ssh_auth_http, username: admin, authorized_keys_url: https://github.com/dhh.keys }

License
-------

Apache 2.0
