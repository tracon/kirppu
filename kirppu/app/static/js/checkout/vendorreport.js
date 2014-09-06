// Generated by CoffeeScript 1.7.1
(function() {
  var states, tables,
    __bind = function(fn, me){ return function(){ return fn.apply(me, arguments); }; },
    __hasProp = {}.hasOwnProperty,
    __extends = function(child, parent) { for (var key in parent) { if (__hasProp.call(parent, key)) child[key] = parent[key]; } function ctor() { this.constructor = child; } ctor.prototype = parent.prototype; child.prototype = new ctor(); child.__super__ = parent.prototype; return child; };

  states = {
    compensable: {
      SO: 'sold'
    },
    returnable: {
      BR: 'on display',
      ST: 'about to be sold'
    },
    other: {
      MI: 'missing',
      RE: 'returned to the vendor',
      CO: 'sold and compensated to the vendor',
      AD: 'not brought to the event'
    }
  };

  tables = [[states.compensable, 'Compensable Items'], [states.returnable, 'Returnable Items'], [states.other, 'Other Items']];

  this.vendorReport = function(vendor) {
    var VendorReport;
    return VendorReport = (function(_super) {
      __extends(VendorReport, _super);

      function VendorReport() {
        this.onGotItems = __bind(this.onGotItems, this);
        return VendorReport.__super__.constructor.apply(this, arguments);
      }

      VendorReport.prototype.title = function() {
        return "Item Report";
      };

      VendorReport.prototype.enter = function() {
        VendorReport.__super__.enter.apply(this, arguments);
        return Api.item_list({
          vendor: vendor.id
        }).done(this.onGotItems);
      };

      VendorReport.prototype.onGotItems = function(items) {
        var name, table, _i, _len, _ref, _results;
        _results = [];
        for (_i = 0, _len = tables.length; _i < _len; _i++) {
          _ref = tables[_i], states = _ref[0], name = _ref[1];
          table = new ItemReportTable(name);
          this.listItems(items, table, states);
          if (table.body.children().length > 0) {
            _results.push(this.cfg.uiRef.body.append(table.render()));
          } else {
            _results.push(void 0);
          }
        }
        return _results;
      };

      VendorReport.prototype.listItems = function(items, table, states) {
        var i, _i, _len, _results;
        _results = [];
        for (_i = 0, _len = items.length; _i < _len; _i++) {
          i = items[_i];
          if (states[i.state] != null) {
            _results.push(table.append(i.code, i.name, displayPrice(i.price), states[i.state]));
          }
        }
        return _results;
      };

      return VendorReport;

    })(CheckoutMode);
  };

}).call(this);
