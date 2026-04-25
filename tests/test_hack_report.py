from onedrive_provisioner.hack_report import build_hack_report


def test_build_hack_report_counts_licenses_and_allocates_costs():
    state = {
        "prefix": "hack-",
        "hackName": "Demo Hack",
        "domain": "contoso.onmicrosoft.com",
        "mode": "team",
        "groups": ["hack-t01-group", "hack-t02-group", "hack-admins"],
        "summary": {"groupsCreated": 3},
        "users": [
            {
                "userPrincipalName": "hack-t01-u01@contoso.onmicrosoft.com",
                "status": "created",
                "isAdmin": False,
                "licenses": ["M365_E3"],
                "groups": ["hack-t01-group"],
            },
            {
                "userPrincipalName": "hack-t01-u02@contoso.onmicrosoft.com",
                "status": "created",
                "isAdmin": False,
                "licenses": ["M365_E3", "COPILOT"],
                "groups": ["hack-t01-group"],
            },
            {
                "userPrincipalName": "hack-t02-u01@contoso.onmicrosoft.com",
                "status": "existing",
                "isAdmin": False,
                "licenses": ["M365_E3"],
                "groups": ["hack-t02-group"],
            },
            {
                "userPrincipalName": "hack-admin01@contoso.onmicrosoft.com",
                "status": "created",
                "isAdmin": True,
                "licenses": [],
                "groups": ["hack-admins"],
            },
        ],
    }

    report = build_hack_report(
        state,
        subscription_costs=[
            {"subscriptionId": "sub-1", "cost": 100, "team": "t01"},
            {"subscriptionId": "sub-2", "cost": 50, "team": "t02"},
        ],
        license_unit_costs={"M365_E3": 10, "COPILOT": 20},
        currency="USD",
    )

    assert report["summary"]["totalUsers"] == 4
    assert report["summary"]["participantUsers"] == 3
    assert report["summary"]["adminUsers"] == 1
    assert report["summary"]["createdUsers"] == 2
    assert report["summary"]["createdAdmins"] == 1
    assert report["licenses"]["totalAssignments"] == 4
    assert report["licenses"]["estimatedMonthlyCost"] == 50
    assert report["subscriptions"]["estimatedPeriodCost"] == 150
    assert report["costs"]["totalEstimated"] == 200

    t01 = next(t for t in report["teams"] if t["team"] == "t01")
    assert t01["users"] == 2
    assert t01["subscriptionCost"] == 100
    assert t01["licenseCost"] == 40

    user = next(u for u in report["users"] if u["userPrincipalName"].endswith("t01-u02@contoso.onmicrosoft.com"))
    assert user["licenseCost"] == 30
    assert user["subscriptionCost"] == 50
    assert user["totalEstimatedCost"] == 80
    assert "password" not in user
    assert "tap" not in user
