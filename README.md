# Ansible Roles

This is a repository of my own reusable ansible roles.

## Usage

### Download the roles

```sh
$ ansible-galaxy install https://github.com/kriansa/ansible-roles
```

### Use roles as a playbook dependency

Your `requirements.yml` is used by the `ansible-galaxy` tool to understand what sources to use. It
should list *this* github repository as the source.


```yaml
---
- src: https://github.com/kriansa/ansible-roles/
  name: kriansa
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
    - kriansa/os-base
```


## License

3-Clause BSD.
