// Generated by CoffeeScript 1.7.1
(function() {
  var __hasProp = {}.hasOwnProperty,
    __extends = function(child, parent) { for (var key in parent) { if (__hasProp.call(parent, key)) child[key] = parent[key]; } function ctor() { this.constructor = child; } ctor.prototype = parent.prototype; child.prototype = new ctor(); child.__super__ = parent.prototype; return child; };

  this.VendorList = (function(_super) {
    __extends(VendorList, _super);

    function VendorList() {
      VendorList.__super__.constructor.apply(this, arguments);
      this.head.append(['<th class="receipt_index">#</th>', '<th class="receipt_code">id</th>', '<th class="receipt_item">name</th>', '<th class="receipt_item">email</th>', '<th class="receipt_item">phone</th>'].map($));
    }

    return VendorList;

  })(ResultTable);

}).call(this);
