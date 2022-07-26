import logging
from collections import namedtuple

from odoo import _, _lt, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


ROUTE_NAMES = {
    'one_step': _lt('Receive in 1 step (stock)'),
    'two_steps': _lt('Receive in 2 steps (input + stock)'),
    'three_steps': _lt('Receive in 3 steps (input + quality + stock)'),
    'pick_ship': _lt('Deliver in 2 steps (pick + ship)'),
    'pick_pack_ship': _lt('Deliver in 3 steps (pick + pack + ship)'),
}


class Warehouse(models.Model):
    _name = "stock.warehouse"
    _description = "Warehouse"
    _order = 'sequence,id'
    _check_company_auto = True
    
    Routing = namedtuple('Routing', ['from_loc', 'dest_loc', 'picking_type', 'action'])

    def _default_name(self):
        count = self.env['stock.warehouse'].with_context(active_test=False).search_count([('company_id', '=', self.env.company.id)])
        return "%s - warehouse # %s" % (self.env.company.name, count + 1) if count else self.env.company.name

    name = fields.Char('Warehouse', index=True, required=True, default=_default_name)
    active = fields.Boolean('Active', default=True)
    company_id = fields.Many2one(
        'res.company', 'Company', default=lambda self: self.env.company,
        index=True, readonly=True, required=True,
        help='The company is automatically set from your user preferences.')
    partner_id = fields.Many2one('res.partner', 'Address', default=lambda self: self.env.company.partner_id, check_company=True)
    view_location_id = fields.Many2one(
        'stock.location', 'View Location',
        domain="[('usage', '=', 'view'), ('company_id', '=', company_id)]",
        required=True, check_company=True)
    lot_stock_id = fields.Many2one(
        'stock.location', 'Location Stock',
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        required=True, check_company=True)
    code = fields.Char('Short Name', required=True, size=5, help="Short name used to identify your warehouse")
    route_ids = fields.Many2many(
        'stock.location.route', 'stock_route_warehouse', 'warehouse_id', 'route_id',
        'Routes',
        domain="[('warehouse_selectable', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help='Defaults routes through the warehouse', check_company=True)
    delivery_steps = fields.Selection([
        ('ship_only', 'Deliver goods directly (1 step)'),
        ('pick_ship', 'Send goods in output and then deliver (2 steps)'),
        ('pick_pack_ship', 'Pack goods, send goods in output and then deliver (3 steps)')],
        'Outgoing Shipments', default='ship_only', required=True,
        help="Default outgoing route to follow")
    wh_input_stock_loc_id = fields.Many2one('stock.location', 'Input Location', check_company=True)
    wh_qc_stock_loc_id = fields.Many2one('stock.location', 'Quality Control Location', check_company=True)
    wh_output_stock_loc_id = fields.Many2one('stock.location', 'Output Location', check_company=True)
    wh_pack_stock_loc_id = fields.Many2one('stock.location', 'Packing Location', check_company=True)
    mto_pull_id = fields.Many2one('stock.rule', 'MTO rule')
    pick_type_id = fields.Many2one('stock.picking.type', 'Pick Type', check_company=True)
    pack_type_id = fields.Many2one('stock.picking.type', 'Pack Type', check_company=True)
    out_type_id = fields.Many2one('stock.picking.type', 'Out Type', check_company=True)
    in_type_id = fields.Many2one('stock.picking.type', 'In Type', check_company=True)
    int_type_id = fields.Many2one('stock.picking.type', 'Internal Type', check_company=True)
    return_type_id = fields.Many2one('stock.picking.type', 'Return Type', check_company=True)
    crossdock_route_id = fields.Many2one('stock.location.route', 'Crossdock Route', ondelete='restrict')
    reception_route_id = fields.Many2one('stock.location.route', 'Receipt Route', ondelete='restrict')
    delivery_route_id = fields.Many2one('stock.location.route', 'Delivery Route', ondelete='restrict')
    resupply_wh_ids = fields.Many2many(
        'stock.warehouse', 'stock_wh_resupply_table', 'supplied_wh_id', 'supplier_wh_id',
        'Resupply From', help="Routes will be created automatically to resupply this warehouse from the warehouses ticked")
    resupply_route_ids = fields.One2many(
        'stock.location.route', 'supplied_wh_id', 'Resupply Routes',
        help="Routes will be created for these resupply warehouses and you can select them on products and product categories")
    sequence = fields.Integer(default=10,
        help="Gives the sequence of this line when displaying the warehouses.")
    _sql_constraints = [
        ('warehouse_name_uniq', 'unique(name, company_id)', 'The name of the warehouse must be unique per company!'),
        ('warehouse_code_uniq', 'unique(code, company_id)', 'The short name of the warehouse must be unique per company!'),
    ]

    @api.onchange('company_id')
    def _onchange_company_id(self):
        group_user = self.env.ref('base.group_user')
        group_stock_multi_warehouses = self.env.ref('stock.group_stock_multi_warehouses')
        group_stock_multi_location = self.env.ref('stock.group_stock_multi_locations')
        if group_stock_multi_warehouses not in group_user.implied_ids and group_stock_multi_location not in group_user.implied_ids:
            return {
                'warning': {
                    'title': _('Warning'),
                    'message': _('Creating a new warehouse will automatically activate the Storage Locations setting')
                }
            }

    @api.model
    def create(self, vals):
        loc_vals = {'name': vals.get('code'), 'usage': 'view',
                    'location_id': self.env.ref('stock.stock_location_locations').id}
        if vals.get('company_id'):
            loc_vals['company_id'] = vals.get('company_id')
        vals['view_location_id'] = self.env['stock.location'].create(loc_vals).id
        sub_locations = self._get_locations_values(vals)

        for field_name, values in sub_locations.items():
            values['location_id'] = vals['view_location_id']
            if vals.get('company_id'):
                values['company_id'] = vals.get('company_id')
            vals[field_name] = self.env['stock.location'].with_context(active_test=False).create(values).id

        warehouse = super(Warehouse, self).create(vals)
        
        new_vals = warehouse._create_or_update_sequences_and_picking_types()
        warehouse.write(new_vals)  
        
        
        route_vals = warehouse._create_or_update_route()
        warehouse.write(route_vals)

        warehouse._create_or_update_global_routes_rules()

        warehouse.create_resupply_routes(warehouse.resupply_wh_ids)

        # if partner assigned updating partner data 
        if vals.get('partner_id'):
            self._update_partner_data(vals['partner_id'], vals.get('company_id'))

        self._check_multiwarehouse_group()

        return warehouse