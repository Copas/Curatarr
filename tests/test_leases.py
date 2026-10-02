from curatarr.leases import acquire, release


def test_database_lease_has_single_owner(app):
    with app.app_context():
        owner = acquire("library_policy_eval:demo")
        assert owner
        assert acquire("library_policy_eval:demo") is None
        release("library_policy_eval:demo", "different-owner")
        assert acquire("library_policy_eval:demo") is None
        release("library_policy_eval:demo", owner)
        assert acquire("library_policy_eval:demo")
