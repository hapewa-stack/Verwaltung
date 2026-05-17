import os
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nebenkostenabrechnung-dev-key-2024")

basedir = os.path.abspath(os.path.dirname(__file__))
instance_dir = os.path.join(basedir, "instance")
os.makedirs(instance_dir, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(instance_dir, 'verwaltung.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

COST_CATEGORIES = [
    "Heizung",
    "Warmwasser",
    "Kaltwasser",
    "Abwasser",
    "Müll",
    "Gebäudeversicherung",
    "Grundsteuer",
    "Hausmeister",
    "Aufzug",
    "Gartenpflege",
    "Allgemeinstrom",
    "Sonstiges",
]

ALLOCATION_KEYS = [
    ("nach Fläche", "flaeche"),
    ("nach Personen", "personen"),
    ("nach Verbrauch", "verbrauch"),
    ("nach Einheit", "einheit"),
]


class Property(db.Model):
    __tablename__ = "property"
    id = db.Column(db.Integer, primary_key=True)
    address = db.Column(db.String(255), nullable=False)
    total_area = db.Column(db.Float, nullable=False)
    tenants = db.relationship("Tenant", backref="property", lazy=True, cascade="all, delete-orphan")
    settlements = db.relationship("Settlement", backref="property", lazy=True, cascade="all, delete-orphan")

    def active_tenants(self):
        return [t for t in self.tenants if t.move_out_date is None or t.move_out_date >= date.today()]


class Tenant(db.Model):
    __tablename__ = "tenant"
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("property.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    unit_name = db.Column(db.String(100), nullable=False)
    area = db.Column(db.Float, nullable=False)
    persons = db.Column(db.Integer, nullable=False, default=1)
    move_in_date = db.Column(db.Date, nullable=False)
    move_out_date = db.Column(db.Date, nullable=True)
    monthly_advance = db.Column(db.Float, nullable=False, default=0.0)
    tenant_settlements = db.relationship("TenantSettlement", backref="tenant", lazy=True, cascade="all, delete-orphan")


class Settlement(db.Model):
    __tablename__ = "settlement"
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("property.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cost_items = db.relationship("CostItem", backref="settlement", lazy=True, cascade="all, delete-orphan")
    tenant_settlements = db.relationship("TenantSettlement", backref="settlement", lazy=True, cascade="all, delete-orphan")

    def total_costs(self):
        return sum(ci.total_amount for ci in self.cost_items)


class CostItem(db.Model):
    __tablename__ = "cost_item"
    id = db.Column(db.Integer, primary_key=True)
    settlement_id = db.Column(db.Integer, db.ForeignKey("settlement.id"), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    allocation_key = db.Column(db.String(20), nullable=False)


class TenantSettlement(db.Model):
    __tablename__ = "tenant_settlement"
    id = db.Column(db.Integer, primary_key=True)
    settlement_id = db.Column(db.Integer, db.ForeignKey("settlement.id"), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    days_in_period = db.Column(db.Integer, nullable=False)
    total_period_days = db.Column(db.Integer, nullable=False)
    computed_share = db.Column(db.Float, nullable=False)
    advance_paid = db.Column(db.Float, nullable=False)
    result = db.Column(db.Float, nullable=False)

    @property
    def is_nachzahlung(self):
        return self.result > 0

    @property
    def result_label(self):
        if self.result > 0:
            return "Nachzahlung"
        elif self.result < 0:
            return "Guthaben"
        return "Ausgeglichen"


def compute_tenant_share(tenant, settlement, cost_items, all_tenants_in_period):
    period_start = settlement.period_start
    period_end = settlement.period_end
    total_period_days = (period_end - period_start).days + 1

    tenant_start = max(tenant.move_in_date, period_start)
    tenant_end = period_end if tenant.move_out_date is None else min(tenant.move_out_date, period_end)

    if tenant_start > tenant_end:
        return 0.0, 0, total_period_days

    days_in_period = (tenant_end - tenant_start).days + 1
    pro_rata_factor = days_in_period / total_period_days

    prop = settlement.property
    total_share = 0.0

    for ci in cost_items:
        if ci.allocation_key == "flaeche":
            if prop.total_area > 0:
                share = (tenant.area / prop.total_area) * ci.total_amount
            else:
                share = 0.0
        elif ci.allocation_key == "einheit":
            unit_count = len(all_tenants_in_period)
            if unit_count > 0:
                share = ci.total_amount / unit_count
            else:
                share = 0.0
        elif ci.allocation_key == "personen":
            total_persons = sum(t.persons for t in all_tenants_in_period)
            if total_persons > 0:
                share = (tenant.persons / total_persons) * ci.total_amount
            else:
                share = 0.0
        elif ci.allocation_key == "verbrauch":
            # "nach Verbrauch" falls back to area-based when no meter data is stored
            if prop.total_area > 0:
                share = (tenant.area / prop.total_area) * ci.total_amount
            else:
                share = 0.0
        else:
            share = 0.0

        total_share += share * pro_rata_factor

    return total_share, days_in_period, total_period_days


@app.route("/")
def index():
    properties = Property.query.order_by(Property.address).all()
    recent_settlements = (
        Settlement.query.order_by(Settlement.created_at.desc()).limit(10).all()
    )
    return render_template("index.html", properties=properties, recent_settlements=recent_settlements)


@app.route("/property/new", methods=["GET", "POST"])
def property_new():
    if request.method == "POST":
        address = request.form.get("address", "").strip()
        total_area_str = request.form.get("total_area", "").strip()
        if not address:
            flash("Adresse ist erforderlich.", "danger")
            return render_template("property_new.html")
        try:
            total_area = float(total_area_str.replace(",", "."))
            if total_area <= 0:
                raise ValueError
        except (ValueError, AttributeError):
            flash("Bitte geben Sie eine gültige Gesamtfläche ein.", "danger")
            return render_template("property_new.html")

        prop = Property(address=address, total_area=total_area)
        db.session.add(prop)
        db.session.commit()
        flash(f'Objekt "{address}" wurde erfolgreich angelegt.', "success")
        return redirect(url_for("index"))
    return render_template("property_new.html")


@app.route("/tenant/new", methods=["GET", "POST"])
def tenant_new():
    properties = Property.query.order_by(Property.address).all()
    if request.method == "POST":
        property_id = request.form.get("property_id")
        name = request.form.get("name", "").strip()
        unit_name = request.form.get("unit_name", "").strip()
        area_str = request.form.get("area", "").strip()
        persons_str = request.form.get("persons", "1").strip()
        move_in_str = request.form.get("move_in_date", "").strip()
        move_out_str = request.form.get("move_out_date", "").strip()
        advance_str = request.form.get("monthly_advance", "0").strip()

        errors = []
        if not property_id:
            errors.append("Bitte wählen Sie ein Objekt aus.")
        if not name:
            errors.append("Name des Mieters ist erforderlich.")
        if not unit_name:
            errors.append("Einheitenbezeichnung ist erforderlich.")

        try:
            area = float(area_str.replace(",", "."))
            if area <= 0:
                raise ValueError
        except (ValueError, AttributeError):
            errors.append("Bitte geben Sie eine gültige Wohnfläche ein.")
            area = 0.0

        try:
            persons = int(persons_str)
            if persons < 1:
                raise ValueError
        except (ValueError, AttributeError):
            errors.append("Personenzahl muss mindestens 1 sein.")
            persons = 1

        try:
            move_in_date = datetime.strptime(move_in_str, "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            errors.append("Bitte geben Sie ein gültiges Einzugsdatum ein.")
            move_in_date = None

        move_out_date = None
        if move_out_str:
            try:
                move_out_date = datetime.strptime(move_out_str, "%Y-%m-%d").date()
            except ValueError:
                errors.append("Bitte geben Sie ein gültiges Auszugsdatum ein.")

        try:
            monthly_advance = float(advance_str.replace(",", "."))
            if monthly_advance < 0:
                raise ValueError
        except (ValueError, AttributeError):
            errors.append("Bitte geben Sie eine gültige Vorauszahlung ein.")
            monthly_advance = 0.0

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("tenant_new.html", properties=properties)

        tenant = Tenant(
            property_id=int(property_id),
            name=name,
            unit_name=unit_name,
            area=area,
            persons=persons,
            move_in_date=move_in_date,
            move_out_date=move_out_date,
            monthly_advance=monthly_advance,
        )
        db.session.add(tenant)
        db.session.commit()
        flash(f'Mieter "{name}" wurde erfolgreich angelegt.', "success")
        return redirect(url_for("index"))
    return render_template("tenant_new.html", properties=properties)


@app.route("/settlement/new")
def settlement_new():
    properties = Property.query.order_by(Property.address).all()
    current_year = date.today().year
    years = list(range(current_year, current_year - 11, -1))
    return render_template(
        "settlement_new.html",
        properties=properties,
        years=years,
        categories=COST_CATEGORIES,
        allocation_keys=ALLOCATION_KEYS,
        current_year=current_year,
    )


@app.route("/settlement/create", methods=["POST"])
def settlement_create():
    property_id = request.form.get("property_id")
    year_str = request.form.get("year")
    period_start_str = request.form.get("period_start", "").strip()
    period_end_str = request.form.get("period_end", "").strip()

    errors = []

    if not property_id:
        errors.append("Bitte wählen Sie ein Objekt aus.")
        prop = None
    else:
        prop = Property.query.get(int(property_id))
        if not prop:
            errors.append("Das ausgewählte Objekt wurde nicht gefunden.")

    try:
        year = int(year_str)
    except (ValueError, TypeError):
        errors.append("Bitte wählen Sie ein gültiges Jahr.")
        year = date.today().year

    try:
        period_start = datetime.strptime(period_start_str, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        errors.append("Bitte geben Sie ein gültiges Periodenstart-Datum ein.")
        period_start = None

    try:
        period_end = datetime.strptime(period_end_str, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        errors.append("Bitte geben Sie ein gültiges Periodenende-Datum ein.")
        period_end = None

    if period_start and period_end and period_start >= period_end:
        errors.append("Das Periodenende muss nach dem Periodenstart liegen.")

    categories = request.form.getlist("category[]")
    amounts = request.form.getlist("amount[]")
    alloc_keys = request.form.getlist("allocation_key[]")

    cost_items_data = []
    for i, (cat, amt_str, alloc) in enumerate(zip(categories, amounts, alloc_keys)):
        cat = cat.strip()
        amt_str = amt_str.strip().replace(",", ".")
        if not cat or not amt_str:
            continue
        try:
            amt = float(amt_str)
            if amt < 0:
                raise ValueError
        except ValueError:
            errors.append(f"Ungültiger Betrag in Zeile {i + 1}: {amt_str}")
            continue
        cost_items_data.append({"category": cat, "total_amount": amt, "allocation_key": alloc})

    if not cost_items_data:
        errors.append("Bitte fügen Sie mindestens eine Kostenposition hinzu.")

    if errors:
        for e in errors:
            flash(e, "danger")
        properties = Property.query.order_by(Property.address).all()
        current_year = date.today().year
        years = list(range(current_year, current_year - 11, -1))
        return render_template(
            "settlement_new.html",
            properties=properties,
            years=years,
            categories=COST_CATEGORIES,
            allocation_keys=ALLOCATION_KEYS,
            current_year=current_year,
        )

    settlement = Settlement(
        property_id=prop.id,
        year=year,
        period_start=period_start,
        period_end=period_end,
    )
    db.session.add(settlement)
    db.session.flush()

    for item_data in cost_items_data:
        ci = CostItem(
            settlement_id=settlement.id,
            category=item_data["category"],
            total_amount=item_data["total_amount"],
            allocation_key=item_data["allocation_key"],
        )
        db.session.add(ci)

    tenants_in_period = [
        t for t in prop.tenants
        if t.move_in_date <= period_end and (t.move_out_date is None or t.move_out_date >= period_start)
    ]

    for tenant in tenants_in_period:
        advance_months = request.form.get(f"advance_{tenant.id}", "").strip()
        try:
            advance_paid = float(advance_months.replace(",", "."))
        except (ValueError, AttributeError):
            tenant_start = max(tenant.move_in_date, period_start)
            tenant_end = period_end if tenant.move_out_date is None else min(tenant.move_out_date, period_end)
            total_period_days = (period_end - period_start).days + 1
            days_in_period = (tenant_end - tenant_start).days + 1
            months_approx = days_in_period / total_period_days * 12
            advance_paid = round(months_approx) * tenant.monthly_advance

        share, days_in_period, total_period_days = compute_tenant_share(
            tenant, settlement, settlement.cost_items, tenants_in_period
        )

        ts = TenantSettlement(
            settlement_id=settlement.id,
            tenant_id=tenant.id,
            days_in_period=days_in_period,
            total_period_days=total_period_days,
            computed_share=round(share, 2),
            advance_paid=round(advance_paid, 2),
            result=round(share - advance_paid, 2),
        )
        db.session.add(ts)

    db.session.commit()
    flash(f"Abrechnung für {year} wurde erfolgreich erstellt.", "success")
    return redirect(url_for("settlement_view", settlement_id=settlement.id))


@app.route("/settlement/<int:settlement_id>")
def settlement_view(settlement_id):
    settlement = Settlement.query.get_or_404(settlement_id)
    return render_template("settlement_view.html", settlement=settlement)


@app.route("/settlement/<int:settlement_id>/pdf")
def settlement_pdf(settlement_id):
    settlement = Settlement.query.get_or_404(settlement_id)
    return render_template("settlement_view.html", settlement=settlement, print_mode=True)


@app.template_filter("euro")
def euro_filter(value):
    try:
        return f"{float(value):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0,00 €"


@app.template_filter("dateformat")
def dateformat_filter(value):
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
