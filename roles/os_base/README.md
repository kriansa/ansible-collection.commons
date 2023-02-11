OS Base
=========

A basic OS setup for my current RHEL/Debian based Linux baselines.

Role Variables
--------------

* **hostname** - Defines the hostname.
* **install_cloudwatch** - Whether to install AWS CloudWatch Agent. (default=True)
* **update_kernel** - Whether to update Kernel or not when this playbook runs

Example Playbook
----------------

    - hosts: servers
      roles:
         - { role: kriansa/os-base, hostname: my-cool-server }

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

Useful snippets
---------------

For publicly accessible SSH, you might want to install sshguard to protect you from unauthorized
attempts and filling your logs.

```yml
tasks:
  - name: install sshguard
    become: true
    when: install_sshguard == true
    notify: restart sshguard
    ansible.builtin.package: state=latest name=sshguard

handlers:
  - name: restart sshguard
    become: true
    ansible.builtin.systemd: name=sshguard enabled=yes state=restarted
```

For systems publicly exposed to the internet without external firewalls, it might be convenient to
configure firewalld (or ufw) to open ports. Here's a snippet:

```yml
- name: open ports on firewalld
  become: true
  when: "'firewalld' in ansible_facts.packages"
  ansible.posix.firewalld:
    port: "{{ item }}"
    permanent: yes
    immediate: yes
    state: enabled
  with_items:
    - 22/tcp
```

License
-------

3-Clause BSD.
