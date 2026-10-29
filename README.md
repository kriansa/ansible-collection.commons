# Ansible Collection - kriansa.commons

This is a repository of my own reusable ansible roles & plugins.

## Usage

### Download the roles

```sh
$ ansible-galaxy collection install https://github.com/kriansa/ansible-collection.commons
```

### Use roles as a playbook dependency

Your `requirements.yml` is used by the `ansible-galaxy` tool to understand what sources to use. It
should list *this* github repository as the source.


```yaml
---
collections:
  - name: https://github.com/kriansa/ansible-collection.commons
    type: git
    version: master
```

Then you will need to pull all the requirements using the command below:

```sh
$ ansible-galaxy install -r requirements.yml
```


### Using the roles

When calling the roles you just need to ensure the folder structure is followed.

```yaml
- roles:
    - kriansa.commons.os_base
```

## Modules

The `quadlet_app` module that used to live here is now its own collection,
[kriansa.quadlet](https://github.com/kriansa/ansible-collection.quadlet), as
`kriansa.quadlet.quadlet_app`.

## License

Apache 2.0
