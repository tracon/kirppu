import functools
import inspect
import logging
import random

from django.conf import settings

from django.core.exceptions import ValidationError
from django.db import models, transaction, IntegrityError
from django.db.models import Q, F, Count
from django.http.response import (
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import (
    get_object_or_404,
    render,
)
from django.utils.translation import ugettext as _
from django.utils.timezone import now
from ipware.ip import get_ip

from .api.common import (
    get_item_or_404 as _get_item_or_404,
    item_state_conflict as _item_state_conflict,
    get_receipt,
)
from .provision import Provision
from .models import (
    Item,
    ItemType,
    Receipt,
    Clerk,
    Counter,
    Event,
    EventPermission,
    ReceiptItem,
    ReceiptExtraRow,
    Vendor,
    ItemStateLog,
    Box,
    TemporaryAccessPermit,
    TemporaryAccessPermitLog,
)
from .fields import ItemPriceField
from .forms import remove_item_from_receipt

from . import ajax_util, stats
from .ajax_util import (
    AjaxError,
    AjaxFunc,
    get_counter,
    get_clerk,
    empty_as_none,
    require_user_features,
    RET_ACCEPTED,
    RET_BAD_REQUEST,
    RET_CONFLICT,
    RET_AUTH_FAILED,
    RET_LOCKED,
)

logger = logging.getLogger(__name__)


def raise_if_item_not_available(item):
    """Raise appropriate AjaxError if item is not in buyable state."""
    if item.state == Item.STAGED:
        # Staged somewhere other?
        raise AjaxError(RET_LOCKED, 'Item is already staged to be sold.')
    elif item.state == Item.ADVERTISED:
        return 'Item has not been brought to event.'
    elif item.state in (Item.SOLD, Item.COMPENSATED):
        raise AjaxError(RET_CONFLICT, 'Item has already been sold.')
    elif item.state == Item.RETURNED:
        raise AjaxError(RET_CONFLICT, 'Item has already been returned to owner.')
    return None


# Registry for ajax functions. Maps function names to AjaxFuncs.
AJAX_FUNCTIONS = {}


def _register_ajax_func(func):
    AJAX_FUNCTIONS[func.name] = func


def ajax_func(url, method='POST', counter=True, clerk=True, overseer=False, atomic=False,
              staff_override=False, ignore_session=False):
    """
    Decorate a function with some common logic.
    The names of the function being decorated are required to be present in the JSON object
    that is passed to the function, and they are automatically decoded and passed to those
    arguments.

    :param url: URL RegEx this function is served in.
    :type url: str
    :param method: HTTP Method required. Default is POST.
    :type method: str
    :param counter: Is registered Counter required? Default: True.
    :type counter: bool
    :param clerk: Is logged in Clerk required? Default: True.
    :type clerk: bool
    :param overseer: Is overseer permission required for Clerk? Default: False.
    :type overseer: bool
    :param atomic: Should this function run in atomic transaction? Default: False.
    :type atomic: bool
    :param staff_override: Whether this function can be called without checkout being active.
    :type staff_override: bool
    :param ignore_session: Whether Event stored in session data should be ignored for the call.
    :return: Decorated function.
    """

    def decorator(func):
        # Get argspec before any decoration.
        spec = inspect.getfullargspec(func)

        wrapped = require_user_features(counter, clerk, overseer, staff_override=staff_override)(func)

        fn = ajax_util.ajax_func(
            func,
            method,
            spec.args[1:],
            spec.defaults,
            staff_override=staff_override,
            ignore_session=ignore_session,
        )(wrapped)
        if atomic:
            fn = transaction.atomic(fn)

        # Copy name etc from original function to wrapping function.
        # The wrapper must be the one referred from urlconf.
        fn = functools.wraps(wrapped)(fn)
        _register_ajax_func(AjaxFunc(fn, url, method))

        return fn
    return decorator


# Must be after ajax_func to ensure circular import works. Must be imported, for part to be included at all in the API.
# noinspection PyUnresolvedReferences
from .api import (
    boxes_api,
    receipt as receipt_api,
)


def checkout_js(request, event_slug):
    """
    Render the JavaScript file that defines the AJAX API functions.
    """
    event = get_object_or_404(Event, slug=event_slug)
    context = {
        'funcs': AJAX_FUNCTIONS.items(),
        'api_name': 'Api',
        'event': event,
    }
    return render(
        request,
        "kirppu/app_ajax_api.js",
        context,
        content_type="application/javascript"
    )


@transaction.atomic
def item_mode_change(request, code, from_, to, message_if_not_first=None):
    if isinstance(code, Item):
        item = code
    else:
        item = _get_item_or_404(code, for_update=True)
    if not isinstance(from_, tuple):
        from_ = (from_,)
    if item.state in from_:
        if item.hidden:
            # If an item is brought to the event, even though the user deleted it, it should begin showing again in
            # users list. The same probably applies to any interaction with the item.
            item.hidden = False

        ItemStateLog.objects.log_state(item=item, new_state=to, request=request)
        old_state = item.state
        item.state = to
        item.save(update_fields=("state", "hidden"))
        ret = item.as_dict()
        if message_if_not_first is not None and len(from_) > 1 and old_state != from_[0]:
            ret.update(_message=message_if_not_first)
        return ret

    else:
        # Item not in expected state.
        _item_state_conflict(item)


@ajax_func('^clerk/login$', clerk=False, counter=False)
def clerk_login(request, event, code, counter):
    try:
        counter_obj = Counter.objects.get(event=event, identifier=counter)
    except Counter.DoesNotExist:
        raise AjaxError(RET_AUTH_FAILED, _(u"Counter has gone missing."))

    try:
        clerk = Clerk.by_code(code, event=event)
    except ValueError as ve:
        raise AjaxError(RET_AUTH_FAILED, repr(ve))

    if clerk is None:
        raise AjaxError(RET_AUTH_FAILED, _(u"No such clerk."))

    clerk_data = clerk.as_dict()
    permissions = EventPermission.get(event, request.user)
    oversee = permissions.can_perform_overseer_actions
    clerk_data['overseer_enabled'] = oversee
    clerk_data['stats_enabled'] = oversee or permissions.can_see_statistics

    active_receipts = Receipt.objects.filter(clerk=clerk, status=Receipt.PENDING, type=Receipt.TYPE_PURCHASE)
    if active_receipts:
        if len(active_receipts) > 1:
            clerk_data["receipts"] = [receipt.as_dict() for receipt in active_receipts]
            clerk_data["receipt"] = "MULTIPLE"
        else:
            receipt = active_receipts[0]
            if "receipt" in request.session:
                logging.warning("Previous receipt reference found in session at login.")
            request.session["receipt"] = receipt.pk
            clerk_data["receipt"] = receipt.as_dict()

    elif "receipt" in request.session:
        logging.warning("Stale receipt reference found in session at login.")
        del request.session["receipt"]

    request.session["clerk"] = clerk.pk
    request.session["clerk_token"] = clerk.access_key
    request.session["counter"] = counter_obj.pk
    request.session["event"] = event.pk
    return clerk_data


@ajax_func('^clerk/logout$', clerk=False, counter=False)
def clerk_logout(request):
    """
    Logout currently logged in clerk.
    """
    # Does not matter which event is being used, the logout shall always succeed.
    clerk_logout_fn(request)
    return HttpResponse()


def clerk_logout_fn(request):
    """
    The actual logout procedure that can be used from elsewhere too.

    :param request: Active request, for session access.
    """
    for key in ["clerk", "clerk_token", "counter", "event", "receipt"]:
        request.session.pop(key, None)


@ajax_func('^counter/validate$', clerk=False, counter=False, ignore_session=True)
def counter_validate(request, event, code):
    """
    Validates the counter identifier and returns its exact form, if it is
    valid.
    """
    try:
        counter = Counter.objects.get(event=event, identifier__iexact=code)
        clerk_logout_fn(request)
    except Counter.DoesNotExist:
        raise AjaxError(RET_AUTH_FAILED)

    return {"counter": counter.identifier,
            "event_name": event.name,
            "name": counter.name}


@ajax_func('^item/find$', method='GET')
def item_find(request, event, code):
    item = _get_item_or_404(code, event=event)
    value = item.as_dict()
    if "available" in request.GET:
        if item.state == Item.STAGED:
            suspended = item.receipt_set.filter(status=Receipt.SUSPENDED, type=Receipt.TYPE_PURCHASE).distinct()
            if len(suspended) == 1:
                suspended = suspended[0]
                value.update(receipt=suspended.as_dict())
                return JsonResponse(
                    value,
                    status=RET_LOCKED,
                    content_type='application/json',
                )

        message = raise_if_item_not_available(item)
        if message is not None:
            value.update(_message=message)
    return value


@ajax_func('^item/search$', method='GET', overseer=True)
def item_search(request, event, query, code, vendor, min_price, max_price, item_type, item_state, show_hidden):

    clauses = []
    name_clauses = []
    description_clauses = []

    types = item_type.split()
    if types:
        clauses.append(Q(itemtype__key__in=types))

    code = code.strip()
    if code:
        clauses.append(Q(code__contains=code))

    if vendor:
        clauses.append(Q(vendor=vendor))

    states = item_state.split()
    if states:
        clauses.append(Q(state__in=states))

    for part in query.split():
        p = Q(name__icontains=part)
        d = Q(box__description__icontains=part)
        if Item.is_item_barcode(part):
            p |= Q(code=part)
            d |= Q(box__representative_item__code=part)
        name_clauses.append(p)
        description_clauses.append(d)

    try:
        clauses.append(Q(price__gte=float(min_price)))
    except ValueError:
        pass

    try:
        clauses.append(Q(price__lte=float(max_price)))
    except ValueError:
        pass

    if show_hidden not in ("true", "1", "on"):
        clauses.append(Q(hidden=False))

    item_clauses = clauses + name_clauses
    box_clauses = clauses + description_clauses
    results = []

    for item in (
        Item.objects
        .filter(*item_clauses, box__isnull=True, vendor__event=event)
        .select_related("itemtype", "vendor", "vendor__user")
        .all()
    ):
        item_dict = item.as_dict()
        item_dict['vendor'] = item.vendor.as_dict()
        results.append(item_dict)

    box_item_counts = dict(
        Item.objects
            .filter(*box_clauses, box__isnull=False)
            .values("box")
            .annotate(item_count=Count("id"))
            .values_list("box", "item_count")
    )

    for item in (
        Item.objects
        .filter(*box_clauses, box__representative_item__id=F("pk"), vendor__event=event)
        .select_related("box", "itemtype", "vendor", "vendor__user")
        .all()
    ):
        item_dict = item.as_dict()
        item_dict['name'] = item.box.description
        item_dict['vendor'] = item.vendor.as_dict()
        item_dict['box'] = {
            "box_number": item.box.box_number,
            "bundle_size": item.box.bundle_size,
            "item_count": box_item_counts[item.box_id],
        }
        results.append(item_dict)

    return results


@ajax_func('^item/edit$', method='POST', overseer=True, atomic=True)
def item_edit(request, event, code, price, state):
    try:
        price = ItemPriceField().clean(price)
    except ValidationError as v:
        raise AjaxError(RET_BAD_REQUEST, ' '.join(v.messages))

    if state not in {st for (st, _) in Item.STATE}:
        raise AjaxError(RET_BAD_REQUEST, 'Unknown state: {0}'.format(state))

    item = _get_item_or_404(code, for_update=True, event=event)
    if item.box_id is not None:
        raise AjaxError(RET_CONFLICT, "Changing box details is not implemented.")
    updates = set()

    if price != item.price:
        updates.add("price")
        price_editable_states = {
            Item.ADVERTISED,
            Item.BROUGHT,
        }
        if (item.state not in price_editable_states and
                state not in price_editable_states):
            raise AjaxError(
                RET_BAD_REQUEST,
                'Cannot change price in state "{0}"'.format(item.get_state_display())
            )

    if item.state != state:
        updates.add("state")
        unsold_states = {
            Item.ADVERTISED,
            Item.BROUGHT,
            Item.MISSING,
            Item.RETURNED,
        }
        # Removing already sold item from receipt.
        if item.state not in unsold_states and item.state != Item.STAGED and state in unsold_states:
            # Need to remove item from receipt.
            receipt_ids = ReceiptItem.objects.filter(
                action=ReceiptItem.ADD,
                item=item,
            ).values_list('receipt_id', flat=True)

            for receipt_id in receipt_ids:
                remove_item_from_receipt(request, item, receipt_id)
        else:
            raise AjaxError(
                RET_BAD_REQUEST,
                u'Cannot change state from "{0}" to "{1}".'.format(
                    item.get_state_display(), str(dict(Item.STATE)[state])
                )
            )

    item.state = state
    item.price = price
    item.save(update_fields=updates)

    item_dict = item.as_dict()
    item_dict['vendor'] = item.vendor.as_dict()
    return item_dict


@ajax_func('^item/list$', method='GET')
def item_list(request, event, vendor):
    items = (Item.objects
             .filter(vendor__id=vendor, vendor__event=event, box__isnull=True)
             .select_related("itemtype")
             .order_by("name")
             )
    return [i.as_dict() for i in items]


@ajax_func('^vendor/returnable$', method='GET')
def vendor_returnable_items(request, vendor):
    # Items that can be returned with box representative items (without other box items).
    items = Item.objects \
        .exclude(state=Item.ADVERTISED) \
        .filter(Q(vendor__id=vendor) & (Q(box__isnull=True) | Q(box__representative_item__pk=F("pk")))) \
        .select_related("itemtype", "box")

    # Shrink boxes to single representative items with box information.
    boxes = Box.objects \
        .exclude(item__state=Item.ADVERTISED) \
        .filter(item__vendor__id=vendor) \
        .annotate(
            item_count=Count("item"),
            returnable_count=Count(1, filter=Q(item__state=Item.BROUGHT)),
            returned_count=Count(1, Q(item__state=Item.RETURNED)),
        )
    boxes = {b.representative_item_id: b for b in boxes}

    # Merge the two queries to a single response.
    r = []
    for i in items:
        box = boxes.get(i.pk)  # type: Box
        element = i.as_dict()
        if box is not None:
            element.update(
                box={
                    "id": box.id,
                    "description": box.description,
                    "box_number": box.box_number,
                    "item_count": box.item_count,
                    "returnable_count": box.returnable_count,
                    "returned_count": box.returned_count,
                }
            )
        r.append(element)

    return sorted(r, key=lambda e: e["name"])


@ajax_func('^item/compensable', method='GET', atomic=True)
def compensable_items(request, event, vendor):
    vendor = int(vendor)
    vendor_items = (
        Item.objects
        .filter(vendor__id=vendor)
        .select_related("itemtype")
        .order_by("name")
    )

    items_for_compensation = vendor_items.filter(state=Item.SOLD)

    r = dict(items=[i.as_dict() for i in items_for_compensation])

    provision = Provision(vendor_id=vendor, provision_function=event.provision_function)
    if provision.has_provision:
        # DON'T SAVE THESE OBJECTS!
        provision_obj = ReceiptExtraRow(
            type=ReceiptExtraRow.TYPE_PROVISION,
            value=provision.provision,
        )
        r["extras"] = [provision_obj.as_dict()]

        if not provision.provision_fix.is_zero():
            # print(provision.provision_fix, provision.provision_fix.is_zero())

            provision_fixup_obj = ReceiptExtraRow(
                type=ReceiptExtraRow.TYPE_PROVISION_FIX,
                value=provision.provision_fix,
            )
            r["extras"].append(provision_fixup_obj.as_dict())

    return r


@ajax_func('^box/list$', method='GET')
def box_list(request, vendor):
    out_boxes = []
    boxes = (
        Box.objects
        .filter(item__vendor__id=vendor, item__hidden=False)
        .select_related("representative_item", "representative_item__itemtype")
        .annotate(
            count=Count("item"),
            brought=Count("item", Q(item__state__in=(Item.BROUGHT, Item.STAGED, Item.SOLD, Item.RETURNED))),
            sold=Count("item", Q(item__state=Item.SOLD)),
            compensated=Count("item", Q(item__state=Item.COMPENSATED)),
            returnable=Count("item", Q(item__state__in=(Item.BROUGHT, Item.STAGED))),
        )
        .distinct()
    )
    for box in boxes:
        data = box.as_dict(exclude=("item_count",))  # item_count is already resolved more efficiently.
        data["item_count"] = box.count
        data["items_brought_total"] = box.brought
        data["items_sold"] = box.sold
        data["items_compensated"] = box.compensated
        data["items_returnable"] = box.returnable
        out_boxes.append(data)
    return out_boxes


@ajax_func('^item/checkin$', atomic=True)
def item_checkin(request, event, code):
    item = _get_item_or_404(code, for_update=True, event=event)
    if not item.vendor.terms_accepted:
        raise AjaxError(500, _(u"Vendor has not accepted terms!"))

    if item.state != Item.ADVERTISED:
        _item_state_conflict(item)

    left_count = None
    if event.max_brought_items is not None:
        count = 1
        if item.box is not None:
            count = item.box.get_item_count()

        brought_count = Item.objects.filter(vendor=item.vendor, state=Item.BROUGHT).count()
        if brought_count + count > event.max_brought_items:
            raise AjaxError(RET_CONFLICT, _("Too many items brought, limit is %i!") % event.max_brought_items)
        else:
            left_count = event.max_brought_items - brought_count - count

    if item.box is not None:
        # Client did not expect box, but this is a box.
        # Assign box number and return box information to client.
        # Expecting a retry to box_checkin.
        box = item.box
        box.assign_box_number()

        response = item.as_dict()
        response["box"] = box.as_dict()

        # TODO: Consider returning bad request to clearly separate actual success.
        return JsonResponse(response, status=RET_ACCEPTED, reason="OTHER API")

    result = item_mode_change(request, item, Item.ADVERTISED, Item.BROUGHT)
    if left_count is not None:
        result["_item_limit_left"] = left_count
    return result


@ajax_func('^item/checkout$', atomic=True)
def item_checkout(request, event, code, vendor=None):
    item = _get_item_or_404(code, for_update=True, event=event)
    if vendor == "":
        vendor = None
    if vendor is not None:
        vendor_id = int(vendor)
        if item.vendor_id != vendor_id:
            raise AjaxError(RET_LOCKED, _("Someone else's item!"))

    box = item.box
    if box:
        if box.representative_item.pk != item.pk:
            raise AjaxError(RET_CONFLICT,
                            "This is not returnable! Boxes have only one returnable item code which returns all!")
        items = box.get_items().select_for_update().filter(state=Item.BROUGHT)

        ItemStateLog.objects.log_states(item_set=items, new_state=Item.RETURNED, request=request)
        items.update(state=Item.RETURNED)

        box_info = Box.objects \
            .filter(pk=box.pk) \
            .annotate(
                item_count=Count("item"),
                returnable_count=Count(models.Case(models.When(item__state=Item.BROUGHT, then=1),
                                                   output_field=models.IntegerField())),
                returned_count=Count(models.Case(models.When(item__state=Item.RETURNED, then=1),
                                                 output_field=models.IntegerField()))
            ) \
            .values("item_count", "returnable_count", "returned_count")[0]

        ret = box.representative_item.as_dict()
        ret["box"] = {
            "id": box.id,
            "description": box.description,
            "box_number": box.box_number,
            "item_count": box_info["item_count"],
            "returnable_count": box_info["returnable_count"],
            "returned_count": box_info["returned_count"],
            "changed": items.count(),
        }
        return ret
    else:
        return item_mode_change(request, item, (Item.BROUGHT, Item.ADVERTISED), Item.RETURNED,
                                _(u"Item was not brought to event."))


@ajax_func('^item/compensate/start$')
def item_compensate_start(request, event, vendor):
    if "compensation" in request.session:
        raise AjaxError(RET_CONFLICT, _(u"Already compensating"))

    vendor_id = int(vendor)
    vendor_list = list(Vendor.objects.filter(pk=vendor_id, event=event))
    if not vendor_list:
        raise AjaxError(RET_BAD_REQUEST)

    clerk = Clerk.objects.get(pk=request.session["clerk"])
    counter = Counter.objects.get(pk=request.session["counter"])

    receipt = Receipt(
        clerk=clerk,
        counter=counter,
        type=Receipt.TYPE_COMPENSATION,
        vendor=vendor_list[0]
    )
    receipt.save()

    request.session["compensation"] = (receipt.pk, vendor_id)

    return receipt.as_dict()


@ajax_func('^item/compensate$', atomic=True)
def item_compensate(request, event, code):
    if "compensation" not in request.session:
        raise AjaxError(RET_CONFLICT, _(u"No compensation started!"))
    receipt_pk, vendor_id = request.session["compensation"]
    receipt = Receipt.objects.select_for_update().get(pk=receipt_pk, type=Receipt.TYPE_COMPENSATION)

    item = _get_item_or_404(code, vendor=vendor_id, for_update=True, event=event)
    item_dict = item_mode_change(request, item, Item.SOLD, Item.COMPENSATED)

    ReceiptItem.objects.create(item=item, receipt=receipt)
    receipt.calculate_total()
    receipt.save(update_fields=("total",))

    return item_dict


@ajax_func('^item/compensate/end', atomic=True)
def item_compensate_end(request, event):
    if "compensation" not in request.session:
        raise AjaxError(RET_CONFLICT, _(u"No compensation started!"))

    receipt_pk, vendor_id = request.session["compensation"]
    receipt = Receipt.objects.select_for_update().get(pk=receipt_pk, type=Receipt.TYPE_COMPENSATION)

    provision = Provision(vendor_id=vendor_id, provision_function=event.provision_function, receipt=receipt)
    if provision.has_provision:
        ReceiptExtraRow.objects.create(
            type=ReceiptExtraRow.TYPE_PROVISION,
            value=provision.provision,
            receipt=receipt,
        )

        if not provision.provision_fix.is_zero():
            ReceiptExtraRow.objects.create(
                type=ReceiptExtraRow.TYPE_PROVISION_FIX,
                value=provision.provision_fix,
                receipt=receipt,
            )

    receipt.status = Receipt.FINISHED
    receipt.end_time = now()
    receipt.calculate_total()
    receipt.save(update_fields=("status", "end_time", "total"))

    del request.session["compensation"]

    return receipt.as_dict()


@ajax_func('^vendor/get$', method='GET')
def vendor_get(request, event, id=None, code=None):
    id = empty_as_none(id)
    code = empty_as_none(code)

    if id is None and code is None:
        raise AjaxError(RET_BAD_REQUEST, "Either id or code must be given")
    if id and code:
        raise AjaxError(RET_BAD_REQUEST, "Only id or code must be given")

    if code:
        id = _get_item_or_404(code, event=event).vendor_id

    try:
        vendor = Vendor.objects.get(pk=int(id))
    except (ValueError, Vendor.DoesNotExist):
        raise AjaxError(RET_BAD_REQUEST, _(u"Invalid vendor id"))
    else:
        return vendor.as_dict()


@ajax_func('^vendor/find$', method='GET')
def vendor_find(request, event, q):
    clauses = [Q(event=event)]
    for part in q.split():
        try:
            clause = Q(id=int(part))
        except ValueError:
            clause = Q()

        clause = clause | (
            Q(user__username__icontains=part) |
            Q(user__first_name__icontains=part) |
            Q(user__last_name__icontains=part) |
            Q(user__email__icontains=part)
        )
        clause = clause | (Q(person__isnull=False) & (
            Q(person__first_name__icontains=part) |
            Q(person__last_name__icontains=part) |
            Q(person__email__icontains=part)
        ))

        clauses.append(clause)

    return [
        v.as_dict()
        for v in (
            Vendor.objects
            .filter(*clauses)
            .select_related("user", "person")
            .all()
        )
    ]


@ajax_func('^vendor/token/create$', method='POST', atomic=True)
def vendor_token_create(request, vendor_id):
    clerk = get_clerk(request)
    vendor = Vendor.objects.get(id=int(vendor_id))

    old_permits = TemporaryAccessPermit.objects.select_for_update().filter(vendor=vendor)
    for permit in old_permits:
        TemporaryAccessPermitLog.objects.create(
            permit=permit,
            action=TemporaryAccessPermitLog.ACTION_INVALIDATE,
            address=get_ip(request),
            peer="{0}/{1}".format(clerk.user.username, clerk.pk),
        )
    old_permits.update(state=TemporaryAccessPermit.STATE_INVALIDATED)

    numbers = settings.KIRPPU_SHORT_CODE_LENGTH
    permit, code = None, None
    for retry in range(60):
        try:
            code = random.randint(10 ** (numbers - 1), 10 ** numbers - 1)
            permit = TemporaryAccessPermit.objects.create(
                vendor=vendor,
                creator=clerk,
                short_code=str(code),
            )
            TemporaryAccessPermitLog.objects.create(
                permit=permit,
                action=TemporaryAccessPermitLog.ACTION_ADD,
                address=get_ip(request),
                peer="{0}/{1}".format(clerk.user.username, clerk.pk),
            )
            break
        except IntegrityError as e:
            continue
    if permit and code:
        return {
            "code": code,
        }
    else:
        raise AjaxError(RET_CONFLICT, _("Gave up code generation."))


@ajax_func('^receipt/start$', atomic=True)
def receipt_start(request):
    if "receipt" in request.session:
        raise AjaxError(RET_CONFLICT, "There is already an active receipt on this counter!")

    clerk = get_clerk(request)
    if Receipt.objects.filter(clerk=clerk, status=Receipt.PENDING, type=Receipt.TYPE_PURCHASE).count() > 0:
        raise AjaxError(RET_CONFLICT, "There is already an active receipt!")

    receipt = Receipt()
    receipt.clerk = clerk
    receipt.counter = get_counter(request)
    receipt.type = Receipt.TYPE_PURCHASE

    receipt.save()

    request.session["receipt"] = receipt.pk
    return receipt.as_dict()


@ajax_func('^item/reserve$', atomic=True)
def item_reserve(request, event, code):
    item = _get_item_or_404(code, for_update=True, event=event)
    if item.box_id is not None:
        raise AjaxError(RET_CONFLICT, "A box cannot be reserved")
    receipt_id = request.session.get("receipt")
    if receipt_id is None:
        raise AjaxError(RET_BAD_REQUEST, "No active receipt found")
    receipt = get_receipt(receipt_id, for_update=True)

    message = raise_if_item_not_available(item)
    if item.state in (Item.ADVERTISED, Item.BROUGHT, Item.MISSING):
        ItemStateLog.objects.log_state(item, Item.STAGED, request=request)
        item.state = Item.STAGED
        item.save(update_fields=("state",))

        ReceiptItem.objects.create(item=item, receipt=receipt)
        # receipt.items.create(item=item)
        receipt.calculate_total()
        receipt.save(update_fields=("total",))

        ret = item.as_dict()
        ret.update(total=receipt.total_cents)
        if message is not None:
            ret.update(_message=message)
        return ret
    else:
        # Not in expected state.
        raise AjaxError(RET_CONFLICT)


@ajax_func('^item/release$', atomic=True)
def item_release(request, code):
    item = _get_item_or_404(code, for_update=True)
    receipt_id = request.session.get("receipt")
    if receipt_id is None:
        raise AjaxError(RET_BAD_REQUEST, "No active receipt found")
    try:
        removal_entry = remove_item_from_receipt(request, item, receipt_id)
    except ValueError as e:
        raise AjaxError(RET_CONFLICT, e.args[0])

    return removal_entry.as_dict()


def _get_active_receipt(request, id, allowed_states=(Receipt.PENDING,)):
    arg_id = int(id)
    in_session = "receipt" in request.session
    if in_session:
        receipt_id = request.session["receipt"]
        if receipt_id != arg_id:
            msg = "Receipt id conflict: {} != {}".format(receipt_id, arg_id)
            logger.error(msg)
            raise AjaxError(RET_CONFLICT, msg)
    else:
        receipt_id = arg_id
        logger.warning("Active receipt is being read without it being in session: %i", receipt_id)

    receipt = get_receipt(receipt_id, for_update=True)
    if receipt.status not in allowed_states:
        if not in_session and receipt.status == Receipt.FINISHED:
            raise AjaxError(RET_CONFLICT, "Receipt {} was already ended at {}".format(receipt_id, receipt.end_time))
        raise AjaxError(RET_CONFLICT, "Receipt {} is in unexpected state: {}".format(
            receipt_id, receipt.get_status_display()))
    return receipt, receipt_id


@ajax_func('^receipt/finish$', atomic=True)
def receipt_finish(request, id):
    receipt, receipt_id = _get_active_receipt(request, id)

    receipt.end_time = now()
    receipt.status = Receipt.FINISHED
    receipt.save(update_fields=("end_time", "status"))

    receipt_items = Item.objects.select_for_update().filter(receipt=receipt, receiptitem__action=ReceiptItem.ADD)
    ItemStateLog.objects.log_states(item_set=receipt_items, new_state=Item.SOLD, request=request)
    receipt_items.update(state=Item.SOLD)

    del request.session["receipt"]
    return receipt.as_dict()


@ajax_func('^receipt/abort$', atomic=True)
def receipt_abort(request, id):
    receipt, receipt_id = _get_active_receipt(request, id, (Receipt.PENDING, Receipt.SUSPENDED))

    # For all ADDed items, add REMOVE-entries and return the real Item's back to available.
    added_items = ReceiptItem.objects.select_for_update().filter(receipt_id=receipt_id, action=ReceiptItem.ADD)
    for receipt_item in added_items.only("item"):
        item = receipt_item.item

        ReceiptItem(item=item, receipt=receipt, action=ReceiptItem.REMOVE).save()

        if item.state != Item.BROUGHT:
            ItemStateLog.objects.log_state(item=item, new_state=Item.BROUGHT, request=request)
            item.state = Item.BROUGHT
            item.save()

    # Update ADDed items to be REMOVED_LATER. This must be done after the real Items have
    # been updated, and the REMOVE-entries added, as this will change the result set of
    # the original added_items -query (to always return zero entries).
    added_items.update(action=ReceiptItem.REMOVED_LATER)

    # End the receipt. (Must be done after previous updates, so calculate_total calculates
    # correct sum.)
    receipt.end_time = now()
    receipt.status = Receipt.ABORTED
    receipt.calculate_total()
    receipt.save(update_fields=("end_time", "status", "total"))

    del request.session["receipt"]
    return receipt.as_dict()


def _get_receipt_data_with_items(**kwargs):
    kwargs.setdefault("type", Receipt.TYPE_PURCHASE)
    receipt = get_object_or_404(Receipt, **kwargs)

    data = receipt.as_dict()
    data["items"] = receipt.row_list()
    return data


@ajax_func('^receipt$', method='GET')
def receipt_get(request):
    """
    Find receipt by receipt id or one item in the receipt.
    """
    if "id" in request.GET:
        receipt_id = int(request.GET.get("id"))
        query = {"pk": receipt_id}
        if request.GET.get("type") == "compensation":
            query["type"] = Receipt.TYPE_COMPENSATION
    elif "item" in request.GET:
        item_code = request.GET.get("item")
        query = {
            "receiptitem__item__code": item_code,
            "receiptitem__action": ReceiptItem.ADD,
            "status": Receipt.FINISHED,
        }
    else:
        raise AjaxError(RET_BAD_REQUEST)
    return _get_receipt_data_with_items(**query)


@ajax_func('^receipt/activate$')
def receipt_activate(request):
    """
    Activate previously started pending receipt.
    """
    clerk = request.session["clerk"]
    receipt_id = int(request.POST.get("id"))
    data = _get_receipt_data_with_items(pk=receipt_id, clerk__id=clerk, status=Receipt.PENDING,
                                        type=Receipt.TYPE_PURCHASE)
    request.session["receipt"] = receipt_id
    return data


@ajax_func('^receipt/pending', overseer=True, method='GET')
def receipt_pending(request):
    receipts = Receipt.objects.filter(status__in=(Receipt.PENDING, Receipt.SUSPENDED), type=Receipt.TYPE_PURCHASE)
    return [receipt.as_dict() for receipt in receipts]


@ajax_func('^receipt/compensated', method='GET')
def receipt_compensated(request, vendor):
    receipts = Receipt.objects.filter(
        type=Receipt.TYPE_COMPENSATION,
        vendor_id=int(vendor)
    ).distinct().order_by("start_time")

    return [receipt.as_dict() for receipt in receipts]


@ajax_func('^barcode$', counter=False, clerk=False, staff_override=True)
def get_barcodes(request, codes=None):
    """
    Get barcode images for a code, or list of codes.

    :param codes: Either list of codes, or a string, encoded in Json string.
    :type codes: str
    :return: List of barcode images encoded in data-url.
    :rtype: list[str]
    """
    from .templatetags.kirppu_tags import barcode_dataurl
    from json import loads

    codes = loads(codes)
    if isinstance(codes, str):
        codes = [codes]

    # XXX: This does ignore the width assertion. Beware with style sheets...
    outs = [
        barcode_dataurl(code, "png", None)
        for code in codes
    ]
    return outs


@ajax_func('^item/abandon$')
def items_abandon(request, vendor):
    """
    Set all of the vendor's 'brought to event' and 'missing' items to abandoned
    The view is expected to refresh itself
    """
    Item.objects.filter(
        vendor__id=vendor,
        state__in=(Item.BROUGHT, Item.MISSING),
    ).update(abandoned=True)
    return


@ajax_func('^item/mark_lost$', overseer=True, atomic=True)
def item_mark_lost(request, event, code):
    item = _get_item_or_404(code=code, for_update=True, event=event)
    if item.state == Item.SOLD:
        raise AjaxError(RET_CONFLICT, u"Item is sold!")
    if item.state == Item.STAGED:
        raise AjaxError(RET_CONFLICT, u"Item is staged to be sold!")
    if item.abandoned:
        raise AjaxError(RET_CONFLICT, u"Item is abandoned.")

    item.lost_property = True
    item.save(update_fields=("lost_property",))
    return item.as_dict()


@ajax_func('^stats/sales_data$', method='GET', staff_override=True)
def stats_sales_data(request, event, prices="false"):
    formatter = stats.SalesData(event=event, as_prices=prices == "true")
    log_generator = stats.iterate_logs(formatter)
    return StreamingHttpResponse(log_generator, content_type='text/csv')


@ajax_func('^stats/registration_data$', method='GET', staff_override=True)
def stats_registration_data(request, event, prices="false"):
    formatter = stats.RegistrationData(event=event, as_prices=prices == "true")
    log_generator = stats.iterate_logs(formatter)
    return StreamingHttpResponse(log_generator, content_type='text/csv')


@ajax_func('^stats/group_sales$', method='GET', staff_override=True)
def stats_group_sales_data(request, event, type_id, prices="false"):
    item_type = ItemType.objects.get(event=event, id=int(type_id))
    formatter = stats.SalesData(event=event, as_prices=prices == "true", extra_filter=dict(item__itemtype=item_type))
    log_generator = stats.iterate_logs(formatter)
    return StreamingHttpResponse(log_generator, content_type='text/csv')
