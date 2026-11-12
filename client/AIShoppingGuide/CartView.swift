import SwiftUI

struct CartView: View {
    @Binding var items: [CartItem]
    var onStartShopping: () -> Void = {}
    @State private var editingSpecificationItem: CartSpecificationEditContext?
    @State private var draftSpecificationOptions: [String: String] = [:]
    @State private var specificationEditorDetent: PresentationDetent = .large
    @State private var selectedProduct: Product?
    @State private var selectedItemIDs: Set<String> = []
    @State private var knownItemIDs: Set<String> = []
    @State private var showCheckoutConfirm = false
    @State private var isCheckingOut = false
    @State private var checkoutMessage: String?

    private let cartService = RESTProductService()

    private var itemCount: Int {
        items.reduce(0) { $0 + $1.quantity }
    }

    private var selectedItems: [CartItem] {
        items.filter { selectedItemIDs.contains($0.id) }
    }

    private var selectedItemCount: Int {
        selectedItems.reduce(0) { $0 + $1.quantity }
    }

    private var selectedTotal: Double {
        selectedItems.reduce(0) { partialResult, item in
            partialResult + item.product.priceValue(for: item.selectedOptions) * Double(item.quantity)
        }
    }

    private var selectedTotalText: String {
        formattedPrice(selectedTotal)
    }

    private var currentItemIDs: Set<String> {
        Set(items.map(\.id))
    }

    private var allItemsSelected: Bool {
        !items.isEmpty && currentItemIDs.isSubset(of: selectedItemIDs)
    }

    private var isPartiallySelected: Bool {
        !selectedItemIDs.intersection(currentItemIDs).isEmpty && !allItemsSelected
    }

    private var bottomOverlayHeight: CGFloat {
        AppTheme.cartCheckoutBottomPadding + 72
    }

    /// 货币符号跟商品价格走（亚马逊数据是 $）
    private var currencySymbol: String {
        guard let item = items.first else { return "$" }
        let display = item.product.priceDisplay(for: item.selectedOptions)
        let symbol = display.prefix { !$0.isNumber }
        return symbol.isEmpty ? "$" : String(symbol)
    }

    private func formattedPrice(_ value: Double) -> String {
        "\(currencySymbol)\(String(format: "%.2f", value))"
    }

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                VStack(spacing: 0) {
                    if items.isEmpty {
                        emptyCart
                    } else {
                        ScrollView(showsIndicators: false) {
                            LazyVStack(alignment: .leading, spacing: 0) {
                                ForEach($items) { $item in
                                    CartRow(
                                        item: $item,
                                        isSelected: selectedItemIDs.contains(item.id),
                                        onToggleSelection: {
                                            toggleSelection(for: item.id)
                                        },
                                        onShowDetail: {
                                            selectedProduct = item.product
                                        },
                                        onEditSpecifications: {
                                            openSpecificationEditor(for: item)
                                        },
                                        onQuantityChange: { quantity in
                                            updateQuantity(id: item.id, quantity: quantity)
                                        },
                                        onRemove: {
                                            removeItem(id: item.id)
                                        }
                                    )
                                    .padding(.vertical, 14)
                                    if item.id != items.last?.id {
                                        Divider().overlay(AppTheme.border)
                                            .padding(.leading, 40)
                                    }
                                }
                            }
                            .padding(.horizontal, 16)
                            .padding(.bottom, bottomOverlayHeight + 24)
                        }
                    }
                }

                if !items.isEmpty {
                    checkoutOverlay
                }
            }
            .sheet(item: $editingSpecificationItem) { editingItem in
                CartSpecificationEditorSheet(
                    product: editingItem.product,
                    selectedOptions: $draftSpecificationOptions,
                    onConfirm: confirmSpecificationEdit
                )
                .presentationDetents([.medium, .large], selection: $specificationEditorDetent)
                .presentationDragIndicator(.visible)
                .presentationCornerRadius(28)
            }
            .background(AppTheme.background)
            .navigationTitle(itemCount > 0 ? "Cart (\(itemCount))" : "Cart")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(AppTheme.background, for: .navigationBar)
            .navigationDestination(item: $selectedProduct) { product in
                ProductDetailView(product: product) { product, selectedOptions, quantity in
                    addToCart(product, selectedOptions: selectedOptions, quantity: quantity)
                }
            }
            .onAppear {
                syncSelectionWithItems()
            }
            .onChange(of: items) { _, _ in
                syncSelectionWithItems()
            }
        }
    }

    private var checkoutOverlay: some View {
        CartCheckoutBar(
            selectedCount: selectedItemCount,
            totalText: selectedTotalText,
            isAllSelected: allItemsSelected,
            isPartiallySelected: isPartiallySelected,
            isCheckingOut: isCheckingOut,
            onToggleAll: toggleSelectAll,
            onCheckout: { showCheckoutConfirm = true }
        )
        .confirmationDialog(
            "Place an order for \(selectedItemCount) items totaling \(selectedTotalText)?",
            isPresented: $showCheckoutConfirm,
            titleVisibility: .visible
        ) {
            Button("Place order") { checkout() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Ships to the default address. Your total is confirmed at checkout.")
        }
        .alert(
            checkoutMessage ?? "",
            isPresented: Binding(
                get: { checkoutMessage != nil },
                set: { if !$0 { checkoutMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(AppTheme.surface)
        .overlay(alignment: .top) {
            Rectangle().fill(AppTheme.border).frame(height: 0.5)
        }
        .padding(.bottom, AppTheme.cartCheckoutBottomPadding)
    }

    private var emptyCart: some View {
        VStack(spacing: 14) {
            Image(systemName: "cart")
                .font(.system(size: 34, weight: .light))
                .foregroundStyle(AppTheme.textTertiary)
                .frame(width: 84, height: 84)
                .background(AppTheme.secondary, in: Circle())
            Text("Your cart is empty")
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)
            Text("Items you add from recommendations show up here.")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
                .multilineTextAlignment(.center)
            Button(action: onStartShopping) {
                Text("Start shopping")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.background)
                    .padding(.horizontal, 28)
                    .padding(.vertical, 12)
                    .background(AppTheme.primary, in: Capsule())
            }
            .buttonStyle(.tactile)
            .padding(.top, 6)
        }
        .padding(.horizontal, 32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.bottom, AppTheme.cartCheckoutBottomPadding + 40)
    }

    private func openSpecificationEditor(for item: CartItem) {
        draftSpecificationOptions = normalizedOptions(
            for: item.product,
            selectedOptions: item.selectedOptions
        )
        specificationEditorDetent = .large
        editingSpecificationItem = CartSpecificationEditContext(
            id: item.id,
            product: item.product
        )
    }

    private func confirmSpecificationEdit() {
        guard
            let editingSpecificationItem,
            let sourceIndex = items.firstIndex(where: { $0.id == editingSpecificationItem.id })
        else {
            closeSpecificationEditor()
            return
        }

        let sourceItem = items[sourceIndex]
        let updatedOptions = normalizedOptions(
            for: sourceItem.product,
            selectedOptions: draftSpecificationOptions
        )
        let updatedItem = CartItem(
            product: sourceItem.product,
            selectedOptions: updatedOptions,
            quantity: sourceItem.quantity,
            backendCartItemID: sourceItem.backendCartItemID,
            skuID: sourceItem.product.matchingSKU(for: updatedOptions)?.id ?? sourceItem.product.displaySKU(for: updatedOptions)?.id
        )
        let sourceWasSelected = selectedItemIDs.contains(sourceItem.id)
        let targetWasSelected = selectedItemIDs.contains(updatedItem.id)

        if updatedItem.id != sourceItem.id {
            if let existingIndex = items.firstIndex(where: { $0.id == updatedItem.id }) {
                items[existingIndex].quantity += sourceItem.quantity
                items.remove(at: sourceIndex)
                selectedItemIDs.remove(sourceItem.id)
                if sourceWasSelected || targetWasSelected {
                    selectedItemIDs.insert(updatedItem.id)
                }
            } else {
                items[sourceIndex] = updatedItem
                selectedItemIDs.remove(sourceItem.id)
                if sourceWasSelected {
                    selectedItemIDs.insert(updatedItem.id)
                }
            }
            knownItemIDs = currentItemIDs
            if let cartItemID = sourceItem.backendCartItemID,
               let skuID = updatedItem.skuID {
                syncBackendCart(
                    CartMutationRequest(
                        action: .updateSpecification,
                        productID: sourceItem.product.id,
                        skuID: skuID,
                        cartItemID: cartItemID,
                        selectedOptions: updatedOptions,
                        quantity: sourceItem.quantity
                    )
                )
            }
        }

        closeSpecificationEditor()
    }

    private func closeSpecificationEditor() {
        editingSpecificationItem = nil
        draftSpecificationOptions = [:]
    }

    private func addToCart(
        _ product: Product,
        selectedOptions: [String: String],
        quantity: Int
    ) {
        let normalizedSelectedOptions = normalizedOptions(
            for: product,
            selectedOptions: selectedOptions
        )
        let item = CartItem(
            product: product,
            selectedOptions: normalizedSelectedOptions,
            quantity: quantity,
            skuID: product.matchingSKU(for: normalizedSelectedOptions)?.id ?? product.displaySKU(for: normalizedSelectedOptions)?.id
        )
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            items[index].quantity += quantity
        } else {
            items.append(item)
        }
        selectedItemIDs.insert(item.id)
        syncBackendCart(
            CartMutationRequest(
                action: .add,
                productID: product.id,
                skuID: item.skuID,
                selectedOptions: normalizedSelectedOptions,
                quantity: quantity
            )
        )
    }

    private func toggleSelection(for id: String) {
        if selectedItemIDs.contains(id) {
            selectedItemIDs.remove(id)
        } else {
            selectedItemIDs.insert(id)
        }
    }

    private func toggleSelectAll() {
        if allItemsSelected {
            selectedItemIDs.subtract(currentItemIDs)
        } else {
            selectedItemIDs.formUnion(currentItemIDs)
        }
    }

    private func removeItem(id: String) {
        guard let item = items.first(where: { $0.id == id }) else { return }
        items.removeAll { $0.id == id }
        selectedItemIDs.remove(id)
        knownItemIDs.remove(id)
        if let cartItemID = item.backendCartItemID {
            syncBackendCart(
                CartMutationRequest(action: .remove, cartItemID: cartItemID),
                restoreOnFailure: item
            )
        }
    }

    private func updateQuantity(id: String, quantity: Int) {
        guard let item = items.first(where: { $0.id == id }),
              let cartItemID = item.backendCartItemID
        else { return }
        syncBackendCart(
            CartMutationRequest(
                action: .updateQuantity,
                cartItemID: cartItemID,
                quantity: quantity
            )
        )
    }

    private func checkout() {
        let ids = selectedItems.compactMap(\.backendCartItemID)
        guard !ids.isEmpty, !isCheckingOut else { return }
        // 本地还没同步到后端的行不能下单，否则用户看到的合计和实际下单的对不上
        guard ids.count == selectedItems.count else {
            checkoutMessage = "Some items are still syncing. Try again in a moment."
            return
        }
        isCheckingOut = true
        Task {
            do {
                let result = try await cartService.checkoutAgentCart(cartItemIDs: ids)
                await MainActor.run {
                    applyCartSnapshot(result.cart)
                    checkoutMessage = "Order placed: \(result.order.itemCount) items totaling \(formattedPrice(result.order.total))."
                    isCheckingOut = false
                }
            } catch {
                await MainActor.run {
                    checkoutMessage = "Could not place the order. Try again."
                    isCheckingOut = false
                }
            }
        }
    }

    private func syncBackendCart(
        _ mutation: CartMutationRequest,
        restoreOnFailure: CartItem? = nil
    ) {
        Task {
            do {
                let snapshot = try await cartService.mutateAgentCart(mutation)
                await MainActor.run {
                    applyCartSnapshot(snapshot)
                }
            } catch {
                guard let restoreOnFailure else { return }
                await MainActor.run {
                    if !items.contains(where: { $0.id == restoreOnFailure.id }) {
                        items.append(restoreOnFailure)
                    }
                    syncSelectionWithItems()
                }
            }
        }
    }

    private func applyCartSnapshot(_ snapshot: CartSnapshotPayload) {
        items = snapshot.items.map(CartItem.init(payload:))
        syncSelectionWithItems()
    }

    private func syncSelectionWithItems() {
        let newItemIDs = currentItemIDs.subtracting(knownItemIDs)
        selectedItemIDs.formIntersection(currentItemIDs)
        selectedItemIDs.formUnion(newItemIDs)
        knownItemIDs = currentItemIDs
    }

    private func normalizedOptions(
        for product: Product,
        selectedOptions: [String: String]
    ) -> [String: String] {
        product.specifications.reduce(into: [:]) { result, specification in
            if let selected = selectedOptions[specification.name] {
                result[specification.name] = selected
            } else if let fallback = specification.options.first {
                result[specification.name] = fallback
            }
        }
    }
}

private struct CartSpecificationEditContext: Identifiable {
    let id: String
    let product: Product
}

struct CartRow: View {
    @Binding var item: CartItem
    let isSelected: Bool
    let onToggleSelection: () -> Void
    let onShowDetail: () -> Void
    let onEditSpecifications: () -> Void
    let onQuantityChange: (Int) -> Void
    let onRemove: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            CartSelectionButton(
                isSelected: isSelected,
                accessibilityLabel: isSelected ? "Deselect \(item.product.title)" : "Select \(item.product.title)",
                action: onToggleSelection
            )
            .padding(.top, 30)

            ProductRemoteImage(url: item.product.imageURL, cornerRadius: 8, placeholderIcon: "shippingbox", contentMode: .fit)
                .padding(6)
                .frame(width: 88, height: 88)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(AppTheme.border, lineWidth: 0.5)
                )
                .contentShape(Rectangle())
                .onTapGesture(perform: onShowDetail)
                .accessibilityAddTraits(.isButton)

            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 6) {
                    Text(item.product.title)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: onShowDetail)
                        .accessibilityAddTraits(.isButton)

                    Button(action: onRemove) {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(AppTheme.textTertiary)
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Remove \(item.product.title)")
                }

                if !item.specificationSummary.isEmpty {
                    Button(action: onEditSpecifications) {
                        HStack(spacing: 4) {
                            Text(item.specificationSummary)
                                .lineLimit(1)
                            Image(systemName: "chevron.down")
                                .font(.system(size: 9, weight: .semibold))
                        }
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(AppTheme.secondary, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Change options. Current: \(item.specificationSummary)")
                }

                HStack(alignment: .center, spacing: 10) {
                    Text(item.product.priceDisplay(for: item.selectedOptions))
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: onShowDetail)
                        .accessibilityAddTraits(.isButton)

                    QuantityControl(
                        quantity: Binding(
                            get: { item.quantity },
                            set: { quantity in
                                item.quantity = quantity
                                onQuantityChange(quantity)
                            }
                        )
                    )
                }
            }
        }
        .contentShape(Rectangle())
    }
}

private struct CartSelectionButton: View {
    let isSelected: Bool
    var isMixed: Bool = false
    var title: String?
    let accessibilityLabel: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbolName)
                    .font(.system(size: 21, weight: .regular))
                    .foregroundStyle(isSelected || isMixed ? AppTheme.textPrimary : AppTheme.textTertiary)
                    .frame(width: 26, height: 26)

                if let title {
                    Text(title)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                }
            }
            .frame(minWidth: title == nil ? 30 : 0, minHeight: 38)
            .contentShape(Rectangle())
        }
        .buttonStyle(.tactile)
        .accessibilityLabel(accessibilityLabel)
    }

    private var symbolName: String {
        if isMixed {
            return "minus.circle.fill"
        }
        return isSelected ? "checkmark.circle.fill" : "circle"
    }
}

private struct CartCheckoutBar: View {
    let selectedCount: Int
    let totalText: String
    let isAllSelected: Bool
    let isPartiallySelected: Bool
    let isCheckingOut: Bool
    let onToggleAll: () -> Void
    let onCheckout: () -> Void

    private var hasSelection: Bool {
        selectedCount > 0
    }

    var body: some View {
        HStack(spacing: 12) {
            CartSelectionButton(
                isSelected: isAllSelected,
                isMixed: isPartiallySelected,
                title: "All",
                accessibilityLabel: isAllSelected ? "Deselect all" : "Select all items",
                action: onToggleAll
            )

            Spacer(minLength: 0)

            VStack(alignment: .trailing, spacing: 1) {
                Text("Subtotal")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                Text(totalText)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }

            Button(action: onCheckout) {
                Text(isCheckingOut ? "Placing…" : (hasSelection ? "Checkout (\(selectedCount))" : "Checkout"))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.background)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
                    .background(
                        hasSelection ? AppTheme.primary : AppTheme.textTertiary,
                        in: Capsule()
                    )
            }
            .buttonStyle(.tactile)
            .disabled(!hasSelection || isCheckingOut)
        }
    }
}

private struct CartSpecificationEditorSheet: View {
    let product: Product
    @Binding var selectedOptions: [String: String]
    let onConfirm: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    specificationControls
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 104)
            }
            .background(AppTheme.background)
            .safeAreaInset(edge: .bottom) {
                Button(action: onConfirm) {
                    Text("Save options")
                        .font(.headline)
                        .foregroundStyle(AppTheme.background)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                        .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                .buttonStyle(.tactile)
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 12)
                .background(AppTheme.surface)
            }
            .navigationTitle("Change options")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            ProductRemoteImage(url: product.imageURL, cornerRadius: 8, placeholderIcon: "shippingbox", contentMode: .fit)
                .frame(width: 76, height: 76)

            VStack(alignment: .leading, spacing: 6) {
                Text(product.priceDisplay(for: selectedOptions))
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text(product.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(2)
                Text(selectedSpecificationSummary.isEmpty ? "Choose options" : selectedSpecificationSummary)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .surfacePanel(cornerRadius: 12)
    }

    private var specificationControls: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(product.specifications) { specification in
                VStack(alignment: .leading, spacing: 10) {
                    Text(specification.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)

                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 82), spacing: 10)], spacing: 10) {
                        ForEach(specification.options, id: \.self) { option in
                            CartSpecificationOptionButton(
                                title: option,
                                isSelected: selectedOptions[specification.name] == option
                            ) {
                                selectedOptions[specification.name] = option
                            }
                        }
                    }
                }
            }
        }
        .padding(16)
        .surfacePanel(cornerRadius: 12)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key): \($0.value)" }
            .joined(separator: "  ")
    }
}

private struct CartSpecificationOptionButton: View {
    let title: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .padding(.horizontal, 8)
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(isSelected ? AppTheme.textPrimary : AppTheme.border, lineWidth: isSelected ? 1.5 : 1)
                )
        }
        .buttonStyle(.tactile)
    }
}

struct QuantityControl: View {
    @Binding var quantity: Int
    @State private var isEditing = false
    @State private var draftQuantity = ""
    @FocusState private var isFocused: Bool

    private let minQuantity = 1
    private let maxQuantity = 99

    var body: some View {
        HStack(spacing: 0) {
            quantityButton(systemName: "minus") {
                quantity = max(minQuantity, quantity - 1)
            }
            .disabled(quantity <= minQuantity)
            .opacity(quantity <= minQuantity ? 0.35 : 1)

            Group {
                if isEditing {
                    TextField("", text: $draftQuantity)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.center)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                        .focused($isFocused)
                        .onChange(of: draftQuantity) { _, newValue in
                            let filtered = newValue.filter(\.isNumber)
                            if filtered != newValue {
                                draftQuantity = filtered
                            }
                        }
                        .onChange(of: isFocused) { _, focused in
                            if !focused {
                                commitDraft()
                            }
                        }
                        .toolbar {
                            ToolbarItemGroup(placement: .keyboard) {
                                Spacer()
                                Button("Done") {
                                    commitDraft()
                                    isFocused = false
                                }
                            }
                        }
                } else {
                    Button {
                        beginEditing()
                    } label: {
                        Text("\(quantity)")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Change quantity. Current: \(quantity)")
                }
            }
            .frame(width: 32, height: 28)

            quantityButton(systemName: "plus") {
                quantity = min(maxQuantity, quantity + 1)
            }
            .disabled(quantity >= maxQuantity)
            .opacity(quantity >= maxQuantity ? 0.35 : 1)
        }
        .frame(width: 96, height: 30)
        .overlay(
            Capsule().stroke(AppTheme.border, lineWidth: 1)
        )
    }

    private func quantityButton(systemName: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .frame(width: 32, height: 28)
        }
        .buttonStyle(.tactile)
    }

    private func beginEditing() {
        draftQuantity = "\(quantity)"
        withAnimation(.easeOut(duration: 0.12)) {
            isEditing = true
        }
        DispatchQueue.main.async {
            isFocused = true
        }
    }

    private func commitDraft() {
        let parsed = Int(draftQuantity) ?? quantity
        quantity = min(max(parsed, minQuantity), maxQuantity)
        draftQuantity = "\(quantity)"
        isEditing = false
    }
}
