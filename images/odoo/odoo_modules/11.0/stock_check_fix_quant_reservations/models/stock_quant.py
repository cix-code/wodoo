from odoo.addons import decimal_precision as dp
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from odoo.addons.queue_job.job import job
# env['stock.quant'].fix_quantity(); env.cr.commit()
class StockQuant(models.Model):
    _inherit = 'stock.quant'

    calculated_reservations = fields.Float(compute="_compute_calculated_reservations", store=False)
    needs_fix_reservation = fields.Boolean(compute="_compute_calculated_reservations", store=False, search="_search_needs_fix")
    over_reservation = fields.Boolean(compute="_compute_over_reservation", store=True)

    @api.depends("quantity", "reserved_quantity")
    def _compute_over_reservation(self):
        digits = dp.get_precision('Product Unit of Measure')(self.env.cr)[1]
        for self in self:
            self.over_reservation = round(self.reserved_quantity, digits) > round(self.quantity, digits)

    def _search_needs_fix(self, operator, value):
        digits = dp.get_precision('Product Unit of Measure')(self.env.cr)[1]
        self.env.cr.execute("""
            select
                sm.product_id,
                l.location_id,
                l.lot_id,
                sum(l.product_uom_qty)
            from
                stock_move_line l
            inner join
                stock_move sm
            on
                sm.id = l.move_id
            inner join
                stock_location loc
            on
                loc.id = l.location_id
            where
                sm.state in ('assigned', 'partially_available')
            and
                loc.usage = 'internal'

            group by
                sm.product_id, l.location_id, l.lot_id
            order by
                1, 2, 3

        """)
        ids = []
        for rec in self.env.cr.fetchall():
            product_id, location_id, lot_id, qty = rec
            self.env.cr.execute("""
                select sum(reserved_quantity)
                from stock_quant
                where
                    product_id=%s
                and
                    location_id=%s
                and
                    coalesce(lot_id, 0) = %s
            """.format(digits), (
                product_id,
                location_id,
                lot_id or 0
            ))
            qty2 = self.env.cr.fetchone()[0]
            if qty2 is None and qty:
                print(f"inventory {lot_id}")
                lot = self.env['stock.production.lot'].browse(lot_id)
                self._fix_missing_quant(
                    lot,
                    self.env['product.product'].browse(product_id),
                    location_id,
                    round(qty, digits)
                )
            elif round(qty2 or 0.0, digits) != round(qty or 0.0, digits):
                ids += [product_id]
        return [('product_id', 'in', list(set(ids)))]

    @api.constrains("reserved_quantity", "quantity")
    def _check_over_reservation(self):
        digits = dp.get_precision('Product Unit of Measure')(self.env.cr)[1]
        for self in self:
            if self.location_id.usage == 'internal':
                if round(self.quantity, digits) < round(self.reserved_quantity, digits):
                    raise ValidationError(_("Cannot reserve {} for {}. Available {}.").format(
                        self.reserved_quantity,
                        self.product_id.default_code,
                        self.quantity,
                    ))

    def _compute_calculated_reservations(self):
        digits = dp.get_precision('Product Unit of Measure')(self.env.cr)[1]
        for self in self:
            self.env.cr.execute("""
                select sum(l.product_uom_qty), l.product_uom_id, pt.uom_id
                from stock_move_line l
                inner join stock_move m
                on m.id = l.move_id
                inner join product_product p
                on p.id = l.product_id
                inner join product_template pt
                on pt.id = p.product_tmpl_id
                where l.location_id=%s
                and coalesce(lot_id, 0) =%s
                and l.product_id=%s
                and m.state in ('assigned', 'partially_available')
                group by l.product_uom_id, pt.uom_id
            """, (self.location_id.id, self.lot_id.id or 0, self.product_id.id))
            Uom = self.env['product.uom']

            def convert(x):
                qty, uom_id, product_uom_id = x
                qty = round(Uom.browse(uom_id)._compute_quantity(qty, Uom.browse(product_uom_id), rounding_method='HALF-UP'), digits)
                return qty, uom_id, product_uom_id

            sums = [convert(x) for x in self.env.cr.fetchall()]
            self.calculated_reservations = sum(x[0] for x in sums)
            self.needs_fix_reservation = self.calculated_reservations != self.reserved_quantity

    @job
    def fix_reservation(self):
        breakpoint()
        digits = dp.get_precision('Product Unit of Measure')(self.env.cr)[1]
        self._merge_quants()
        for self in self:
            if self.location_id.usage not in ['internal']:
                continue
            if not self.exists():
                continue
            if self.calculated_reservations > self.quantity:
                self.env['stock.move.line']._model_make_quick_inventory(
                    self.location_id,
                    0,
                    self.product_id,
                    self.lot_id,
                    add=self.calculated_reservations - self.quantity
                )
                self._merge_quants()
            if round(self.reserved_quantity, digits) != round(self.calculated_reservations, digits):
                self.sudo().reserved_quantity = self.calculated_reservations
        self._merge_quants()

    @api.model
    def _fix_all_reservations(self, commit=False):
        quants = self.search([('needs_fix_reservation', '=', True)])
        for i, quant in enumerate(quants):
            print(f"{quant.id} {quant.product_id.default_code} {i} of {len(quants)}")
            if quant.calculated_reservations != quant.reserved_quantity:
                quant.fix_reservation()
                if commit:
                    self.env.cr.commit()

    @api.model
    def _get_status(self, fix, product=None, raise_error=False, expects_stock_at_location=0):
        products = product or self.env['product.product'].search([('type', '=', 'product')])
        for product in products:
            for lot in self.env['stock.production.lot'].search([('product_id', '=', product.id)]):

                self.env.cr.execute("""
                    select sum(product_uom_qty), stock_move_line.location_id, lot_id
                    from stock_move_line
                    inner join stock_location l
                    on l.id = stock_move_line.location_id
                    where lot_id=%s
                    and state in ('assigned', 'partially_available')
                    and l.usage in ('internal')
                    group by stock_move_line.location_id, lot_id
                """, (lot.id,))
                sums = self.env.cr.fetchall()

                # missing the quant for zero stock but reserved
                if not [x for x in sums if x[1] == expects_stock_at_location]:
                    sums += [(0, expects_stock_at_location, lot.id)]

                for S in sums:
                    tries = 0
                    while True:
                        tries += 1

                        self.env.cr.execute("""
                            select reserved_quantity
                            from stock_quant
                            where lot_id=%s and location_id=%s
                        """, (lot.id, S[1]))
                        quants = self.env.cr.fetchall()
                        if len(quants) > 1:
                            if tries > 1:
                                raise Exception(f"Cannot merge duplicate quants {product.default_code}")
                            self._merge_quants()
                        else:
                            break
                    if len(quants) == 0 and S[0]:
                        error = f"Quant missing: {product.default_code}-{lot.name}"
                        if raise_error:
                            raise UserError(error)
                        if fix:
                            self._fix_missing_quant(lot=lot, product=product, location_id=S[1], quantity=S[0])

                    elif quants and quants[0][0] != S[0]:
                        error = f"Reservation deviation: {product.default_code}-{lot.name}"
                        if raise_error: raise UserError(error)
                        if fix:
                            self.fix_reservation()
            self.env.cr.commit()

    def _fix_missing_quant(self, lot, product, location_id, quantity):
        assert product
        assert isinstance(location_id, int)
        location = self.env['stock.location'].browse(location_id)
        inv = self.env['stock.inventory'].create({
            "location_id": location.id,
            "filter": "product",
            "company_id": self.env.user.company_id.id,
            "name": "Fix Quant Inventory {}: {} [{}]".format(location.name, lot.name, product.default_code),
            "product_id": product.id,
        })
        inv.action_start()
        if lot:
            line = inv.line_ids.filtered(lambda x: x.prod_lot_id == lot and x.location_id == location_id)
        else:
            line = inv.line_ids.filtered(lambda x: x.product_id == product and x.location_id == location_id)
        if line:
            quantity += line.product_qty

        inv.line_ids.unlink()
        inv.line_ids = [[0, 0, {
            'prod_lot_id': lot.id,
            'product_id': product.id,
            'location_id': location.id,
            'product_qty': quantity,
        }]]
        inv.action_done()

        # also still broken after that
        if lot.id:
            domain = [
                ('location_id.usage', '=', 'internal'),
                ('product_id', '=', product.id),
            ]
            if lot:
                domain += [('lot_id', '=', lot.id)]
            self.env['stock.quant'].search(domain).fix_reservation()

        return inv
