Container Server
================

This prepares a host whose job is to run containers. It assumes the host sits on two networks: a
**public** one carrying the services the host exists to serve, which owns the only default route,
and a **private** one used for management, where SSH and Cockpit listen and which never becomes the
default route. Both are usually RFC1918 addresses — public and private describe the job each network
does, not their addressing.

On top of that it sets up remote access (SSH restricted to the private address, Cockpit on the same
address), a stratis pool that backs both podman's volumes and its image store, and fish as the login
shell for root and the admin user.

The image store is mounted with `nofail`, so boot does not wait for it. The role therefore makes
every quadlet unit wait for that mount, through drop-ins in `/etc/containers/systemd/<type>.d/`.
Podman clears its record of containers and volumes once per boot, on the first podman command of
any kind. If that command runs before the mount, the clearing lands on a stale copy underneath it,
and podman goes on believing that volumes from the previous boot are still mounted.

Podman itself comes from the `podman` role, which this one depends on.

Role Variables
--------------

Networking:

* **public_network_name** / **private_network_name** - NetworkManager connection names, labels only
* **public_interface** / **private_interface** - the interfaces each network is wired to
* **public_ip** / **private_ip** - addresses, without the prefix
* **public_prefix** / **private_prefix** - netmask prefixes, both default to 24
* **public_gateway** - the host's only default route
* **public_dns** / **private_dns** - resolvers, as lists
* **public_dns_search** / **private_dns_search** - search domains
* **removed_network_connections** - connections to delete, for the auto-generated ones cloud images
  ship

Everything else:

* **username** - the admin user, matching the one `os_base` creates
* **user_password** - crypted password for that user, which Cockpit needs to let anyone log in.
  Empty leaves the existing password alone
* **cockpit_port** - port Cockpit listens on, private network only
* **stratis_pool_name** / **stratis_pool_device** - the pool, and the device it consumes whole
* **podman_volume_stratis_version** - version of the volume plugin backing podman volumes

Example Playbook
----------------

    - hosts: servers
      roles:
        - role: kriansa.commons.os_base
        - role: kriansa.commons.container_server
          public_network_name: Svc
          public_interface: ens18
          public_ip: 192.168.4.22
          public_gateway: 192.168.4.1
          public_dns: [192.168.4.1]
          public_dns_search: svc.lan.cx
          private_network_name: Admin
          private_interface: ens19
          private_ip: 192.168.10.22
          private_dns: [192.168.10.1]
          private_dns_search: admin.lan.cx

License
-------

Apache 2.0
