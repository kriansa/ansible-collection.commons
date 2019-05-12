# Why this folder?

Unfortunately, current `ansible-galaxy` implementation will not allow roles in a monorepo fashion.
Instead, it considers that a single repo maps to exactly one repo. This is slowly changing with the
introduction of a (currently _beta_) CLI that allows that (Mazer), but it's not mature yet. 

For now, I won't be using this, so we will kind of "hack" the way that ansible-galaxy reads a
repository, tricking it to think that it's a single role, but we'll actually have many. This is
possible with the addition of a `meta/main.yml` file.

## References

* https://github.com/ansible/ansible/issues/16804
* https://galaxy.ansible.com/docs/contributing/creating_multi.html
