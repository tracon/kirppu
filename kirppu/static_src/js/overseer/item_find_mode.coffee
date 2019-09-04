class @ItemFindMode extends CheckoutMode
  ModeSwitcher.registerEntryPoint("item_find", @)

  constructor: ->
    super
    @itemList = new ItemFindList()
    @searchForm = new ItemSearchForm(@doSearch)
    @search = null

    @_focusInSearch = false
    @searchForm.searchInput.on("focus", () => @_focusInSearch = true)
    @searchForm.searchInput.on("blur", () => @_focusInSearch = false)

  enter: ->
    super
    @cfg.uiRef.body.empty()
    @cfg.uiRef.body.append(@searchForm.render())
    @cfg.uiRef.body.append(@itemList.render())
    @searchForm.searchInput.focus()

  glyph: -> "search"
  title: -> gettext("Item Search")

  doSearch: (query, code, vendor, min_price, max_price, type, state, show_hidden) =>
    @search =
      query: query
      code: code.toUpperCase()
      vendor: vendor
      min_price: min_price
      max_price: max_price
      item_type: if type? then type.join(' ') else ''
      item_state: if state? then state.join(' ') else ''
      show_hidden: show_hidden
    Api.item_search(@search).done(@onItemsFound)

  onItemsFound: (items) =>
    @itemList.body.empty()
    for item_, index_ in items
      ((item, index) =>
        @itemList.append(
          item,
          index + 1,
          @onItemClick,
        )
      )(item_, index_)
    if items.length == 0
      @itemList.no_results()
    if @_focusInSearch
      @searchForm.searchInput.select()
    return

  onItemClick: (item) =>
    do new ItemEditDialog(item, @onItemSaved).show

  onItemSaved: (item, dialog) =>
    Api.item_edit(item).done((editedItem) =>
      dialog.setItem(editedItem)
      if @search?
        Api.item_search(@search).done(@onItemsFound)
    ).fail((jqXHR) =>
      msg = "Item edit failed (#{jqXHR.status}): #{jqXHR.responseText}"
      dialog.displayError(msg)
    )
