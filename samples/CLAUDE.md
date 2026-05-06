# Fund Product Automation Prompt

## Objective
Automate the process of creating a new fund product in a web application by copying an existing product.

---

## Configuration
Store all runtime values in a separate configuration file (`config.json` or `.env`).

### Required Fields:
- base_url
- username
- password
- screen_id
- existing_product_code (default: MDFD)
- fund_product_excel_file (default: EXCEL/Fund_Product.xlsx)
- fund_product_excel_sheet (default: Pool Products)

---

## Automation Workflow

### 1. Login
- Open browser
- Navigate to base_url
- Enter username and password
- Click **Sign In**

### 2. Post-login Handling
- If informational popup appears, click **OK**

### 3. Navigate to Screen
- Locate screen ID input (top-right corner)
- Enter screen_id from config
- Submit to open the screen

### ~~4. Initiate New Entry~~ *(commented out — not required)*
<!-- - Click **New** button (top-left corner) -->

### 5. Copy Existing Product
- Click **Enter Query**
- Enter existing_product_code (e.g., MDFD)
- Execute query
- Wait for product details

### 6. Create New Product
- Click **Copy**
- Enter new_product_code (e.g., SPCP)
- Enter new_product_description (e.g., Special Mudarabah Pools)
- If any **Override** popup appears, click **Accept**
- Click **Save**

### 7. Validation
- Confirm success message or UI confirmation

### 8. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 9
- If status is **Authorized** (`A`), skip authorization and move to the next product

### 9. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADFNPRD**
- Click **Enter Query**
- Enter the product code in the **Product Code** field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

---

## Fund ID Creation Workflow (fundIdCreation.js)

### 10. Read Fund Data from Excel
- Read `EXCEL/Fund_ID.xlsx`, sheet **Pools**
- Column mapping:
  - **FUND_ID** → Fund ID on screen
  - **PRODUCT** → Fund Product code
  - **FUND_DESCRIPTION** → Fund Description on screen
  - **BASE_CCY** → Base Currency on screen
- Loop through each data row and perform steps 11–19 for every row

### 11. Navigate to Screen IADFNONL
- Enter function ID **IADFNONL** in the top-right corner
- Submit to open the Fund Online screen

### 12. Initiate New Entry
- Click **New** button

### 13. Enter Fund Product
- Enter the **PRODUCT** value from Excel in the Fund Product field
- Click the **Fund Product** button
- A random Fund ID and Fund Reference Number will be generated automatically

### 14. Configure Fund Details
- Change the **Fund ID** to the **FUND_ID** value from Excel
- Enter the **FUND_DESCRIPTION** from Excel in the Fund Description field
- Set the **Base Currency** to the **BASE_CCY** value from Excel
- Uncheck **Profit Calculation Required**
- Set **Profit Distribution Type** to **Variable**
- Set **Face Value** to **0**

### 15. Save Fund
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 16. Validation
- Confirm success message or UI confirmation

### 17. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 18
- If status is **Authorized** (`A`), skip authorization and go to step 19

### 18. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADFNONL**
- Click **Enter Query**
- Enter the **Fund ID** in the Fund ID field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

### 19. Repeat
- Repeat steps 11–18 for the next row in Excel

---

## Account Class Tagging Workflow (accountClassTagging.js)

### 20. Read Account Class Data from Excel
- Read `EXCEL/Account_Class.xlsx`, sheet **Account Class Details**
- Column mapping:
  - **Account Class** → Account Class field on screen
  - **Account Class Type** → filter criteria (only **Saving** and **Term Deposit** rows)
  - **Mudarabah Fund ID** → Mudarabah Fund ID dropdown on Preferences tab
  - **Profit Calculation Balance Basis** → Profit Calculation Balance Basis dropdown on Preferences tab
- Filter: only process rows where **Account Class Type** = "Saving" or "Term Deposit"
- Loop through each filtered row and perform steps 21–28

### 21. Navigate to Screen IADACCLS
- Enter function ID **IADACCLS** in the top-right corner
- Submit to open the Account Class screen

### 22. Query Existing Account Class
- Click **Enter Query**
- Enter the **Account Class** value from Excel in the Account Class field
- Click **Execute Query**
- Wait for account class details to load
- Click **Unlock**

### 23. Configure Preferences Tab
- Navigate to the **Preferences** tab (lower section of the screen)
- Scroll down to the Mudarabah section
- Select the **Mudarabah Fund ID** from the dropdown (value from Excel)
- Select the **Profit Calculation Balance Basis** from the dropdown (value from Excel)

### 24. Save Changes
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 25. Validation
- Confirm success message or UI confirmation

### 26. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 27
- If status is **Authorized** (`A`), skip authorization and go to step 28

### 27. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADACCLS**
- Click **Enter Query**
- Enter the **Account Class** value in the Account Class field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

### 28. Repeat
- Repeat steps 21–27 for the next filtered row in Excel

---

## Liability Product Weightages Workflow (liabilityWeightages.js)

### 29. Read Liability Product Weightages Data from Excel
- Read `EXCEL/Liability Product Weightages.xlsx`
- First read sheet **Mudarib Fee**
- Column mapping for Mudarib Fee sheet:
  - **ACCOUNT_CLASS** → Account Class field on screen
  - **MUDARABAH_FUNDID** → Mudarabah Fund ID field on screen
  - **EFFECTIVE_DATE** → Effective Date field on screen
  - **PRODUCT_TYPE** → Product Type radio button selection on screen (3 radio buttons available)
  - **MUDARIB_FEE** → Fee (%) field on screen
  - **CURRENCY** → Currency Code field on screen
- Group rows by **ACCOUNT_CLASS** — for each unique account class, process the Mudarib Fee record and then its Amount Slab records
- Loop through each unique account class and perform steps 30–40

### 30. Navigate to Screen ICDWTSMT
- Enter function ID **ICDWTSMT** in the top-right corner
- Submit to open the Liability Product Weightages screen

### 31. Initiate New Entry
- Click **New** button

### 32. Enter Account Class and Mudarib Fee Details
- Enter the **ACCOUNT_CLASS** value from the Mudarib Fee sheet in the Account Class field
- Enter the **MUDARABAH_FUNDID** value in the Mudarabah Fund ID field
- Enter the **EFFECTIVE_DATE** value in the Effective Date field
- Select the **PRODUCT_TYPE** radio button that matches the value from Excel (3 radio buttons are present on screen)
- Enter the **MUDARIB_FEE** value in the Fee (%) field
- Enter the **CURRENCY** value in the Currency Code field

### 33. Populate Amount Slab Grid
- Read the **Amount Slab** sheet from the same Excel file
- Filter rows for the current **ACCOUNT_CLASS**
- The screen shows a Value Category label and a grid below it with two columns: **Amount Slab** and **Weight(%)**
- For each row in the Amount Slab sheet matching the current account class:
  - Click the **+** (Add Row) button on the grid
  - Enter the **Amount Slab** value in the Amount Slab field of the new row
  - Enter the **Weight** value in the Weight (%) field of the new row
- Repeat until all Amount Slab rows for this account class are added

### 34. Populate Tenor Grid
- Read the **Tenor** sheet from the same Excel file
- Filter rows for the current **ACCOUNT_CLASS**
- The screen shows a **Tenor** category section with a grid below it
- Column mapping (screen field → Excel column):
  - **Units** (Frequency) → **Frequency**
  - **Tenor** → **Tenor**
  - **TD Weight** → **TD_WEIGHT**
  - **Savings/Current Account** weight → **SAV_WEIGHT**
- For each matching row:
  - Click the **+** (Add Row) button on the Tenor grid
  - Enter the **Frequency** value in the Units field
  - Enter the **Tenor** value in the Tenor field
  - Enter the **TD_WEIGHT** value in the TD Weight field
  - Enter the **SAV_WEIGHT** value in the Savings/Current Account field
- Repeat until all Tenor rows for this account class are added

### 35. Populate PPO Grid
- Read the **PPO** sheet from the same Excel file
- Filter rows for the current **ACCOUNT_CLASS**
- The screen shows a **PPO** category section with a grid below it
- Column mapping (screen field → Excel column):
  - **Days** → **DAYS**
  - **Months** → **MONTHS**
  - **Years** → **YEARS**
  - **Weight** → **WEIGHT**
- For each matching row:
  - Click the **+** (Add Row) button on the PPO grid
  - Enter the **DAYS** value in the Days field
  - Enter the **MONTHS** value in the Months field
  - Enter the **YEARS** value in the Years field
  - Enter the **WEIGHT** value in the Weight field
- Repeat until all PPO rows for this account class are added

### 36. Save Record
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 37. Validation
- Confirm success message or UI confirmation

### 38. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 39
- If status is **Authorized** (`A`), skip authorization and go to step 40

### 39. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **ICDWTSMT**
- Click **Enter Query**
- Enter the **Account Class** value in the Account Class field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

### 40. Repeat
- Repeat steps 30–39 for the next unique account class

---

## Account Class Order to Pool Workflow (accountClassOrder.js)

### 41. Read Account Class Order Data from Excel
- Read `EXCEL/Account_Class_Order.xlsx`, sheet **Account Class Order**
- Column mapping:
  - **POOL_ID** → Pool ID (used to group rows; one screen record per unique Pool ID)
  - **ACCOUNT_CLASS** → Account Class field on screen
  - **BALANCING_ORDER** → Order field on screen
- Group rows by **POOL_ID** — for each unique Pool ID, process all its Account Class rows in one screen record
- Loop through each unique Pool ID and perform steps 42–49

### 42. Navigate to Screen IADCAOPB
- Enter function ID **IADCAOPB** in the top-right corner
- Submit to open the Account Class Order to Pool screen

### 43. Initiate New Entry
- Click **New** button

### 44. Enter Pool ID and Account Class Rows
- Enter the **POOL_ID** value in the Pool ID field on screen
- For each row belonging to this Pool ID:
  - Click the **+** (Add Row) button on the Account Class grid
  - Enter the **ACCOUNT_CLASS** value in the Account Class field of the new row
  - Enter the **BALANCING_ORDER** value in the Order field of the new row
- Repeat until all Account Class rows for this Pool ID are added

### 45. Save Record
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 46. Validation
- Confirm success message or UI confirmation

### 47. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 48
- If status is **Authorized** (`A`), skip authorization and go to step 49

### 48. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADCAOPB**
- Click **Enter Query**
- Enter the **Pool ID** value in the Pool ID field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

### 49. Repeat
- Repeat steps 42–48 for the next unique Pool ID

---

## Equity GL Tagging to Pool Workflow (equityGLTagging.js)

### 50. Read Equity GL Data from Excel
- Read `EXCEL/Islamic_Equity_Gls.xlsx`, sheet **Equity GLs**
- Column mapping:
  - **FUND_ID** → Fund ID (used to group rows; one screen record per unique Fund ID)
  - **GL CODE** → GL Code field on screen
  - **POOL_ORDER** → Order field on screen
- Group rows by **FUND_ID** — for each unique Fund ID, process all its GL Code rows in one screen record
- Loop through each unique Fund ID and perform steps 51–58

### 51. Navigate to Screen IADEQGLM
- Enter function ID **IADEQGLM** in the top-right corner
- Submit to open the Equity GL Tagging to Pool screen

### 52. Initiate New Entry
- Click **New** button
- If a validation popup appears (e.g. "FUND ID NOT INPUT"), dismiss it by clicking **Ok**

### 53. Select Fund ID via LOV
- Click the **LOV button** next to the Fund ID field on screen
- In the LOV popup: click **Fetch** to load all available Fund IDs
- Find and click the row matching the **FUND_ID** value from Excel
- The Fund ID field is populated automatically after LOV selection

### 54. Enter GL Code Rows
- For each GL code row belonging to this Fund ID:
  - Click the **+** (Add Row) button on the GL Code grid
  - Enter the **GL CODE** value from Excel in the **GL Code** field of the new row
  - Enter the **POOL_ORDER** value from Excel in the **Order** field of the new row
- Repeat until all GL Code rows for this Fund ID are added

### 55. Save Record
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 55a. Handle "Record Already Exists" Error
- If FLEXCUBE returns a **"Record Already Exists"** error after Save:
  1. Close the current screen / dismiss the error popup
  2. Click **Enter Query** on the IADEQGLM toolbar
  3. Open the Fund ID LOV, select the matching **FUND_ID**, click **Execute Query** to load the existing record
  4. Check the **Authorization Status** of the loaded record:
     - If **Authorized** (`A`): click **Unlock** to make the record editable before making changes
     - If **Unauthorized** (`U`): the record is already editable — no Unlock needed
  5. Read all GL Codes currently in the screen grid
  6. Compare against the GL Codes for this Fund ID in the Excel file:
     - **Added** (in Excel but NOT on screen): click **+** (Add Row), fill GL Code and Order
     - **Removed** (on screen but NOT in Excel): check that row's checkbox and click **−** (Delete Row)
  7. If no differences found → skip Save and go directly to step 56
  8. If changes were made → Click **Save** + handle any **Override** popup

### 56. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen (bottom area)
- If status is **Unauthorized** (`U`), proceed to step 57
- If status is **Authorized** (`A`), skip authorization and go to step 58

### 57. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADEQGLM**
- Click **Enter Query**
- Open the FUND ID LOV and select the same Fund ID
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

### 58. Repeat
- Repeat steps 51–57 for the next unique Fund ID

---

## Pool Balancing Order Workflow (poolBalancingOrder.js)

### 59. Read Pool Balancing Order Data from Excel
- Read `EXCEL/Pool_Balance_Order.xlsx`, sheet **Pool_Balance_Order**
- Column mapping:
  - **FUND_ID** → Fund ID (LOV field on screen grid row)
  - **BALANCING_ORDER** → Bal Order field on screen grid
  - **GENERAL_HIBA_PERCENTAGE** → Gen Hiba Percentage field on screen grid
  - **FIXED_ASSET_PERCENTAGE** → Fix Asset Percentage field on screen grid
  - **EQUITY_BASE** → Equity Base dropdown on screen grid (select value **Monthly Average**)
  - **IRR_PERCENTAGE** → IRR Percentage field on screen grid
  - **PEA_PERCENTAGE** → PEA Percentage field on screen grid
- Collect all Fund IDs from Excel — these are the rows that must exist in the screen grid

### 60. Navigate to Screen IADPBALO
- Enter function ID **IADPBALO** in the top-right corner
- Submit to open the Pool Balancing Order screen

### 61. Query Existing Record for Pool Group Code FUND
- Click **Enter Query**
- Enter **FUND** in the Pool Group Code field
- Click **Execute Query**
- The screen loads the existing Pool Balancing Order record for the FUND group
- Read the Fund IDs already present in the grid rows

### 62. Identify Missing Fund IDs
- Compare the Fund IDs from Excel with the Fund IDs already in the screen grid
- Any Fund ID from Excel that is NOT already in the grid is considered **missing** and must be added

### 63. Add Missing Fund IDs to the Grid
- For each missing Fund ID:
  - Click the **+** (Add Row) button on the grid
  - Click the **LOV button** next to the Fund ID field in the new row
  - In the LOV popup: click **Fetch** to load all available Fund IDs
  - Find and click the row matching the **FUND_ID** value from Excel
  - Enter the **BALANCING_ORDER** value from Excel in the **Bal Order** field
  - Enter the **GENERAL_HIBA_PERCENTAGE** value in the **Gen Hiba Percentage** field
  - Enter the **FIXED_ASSET_PERCENTAGE** value in the **Fix Asset Percentage** field
  - Select **Monthly Average** from the **Equity Base** dropdown
  - Enter the **IRR_PERCENTAGE** value in the **IRR Percentage** field
  - Enter the **PEA_PERCENTAGE** value in the **PEA Percentage** field
- Repeat until all missing Fund IDs have been added

### 64. Save Record
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 65. Validation
- Confirm success message or UI confirmation

### 66. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 67
- If status is **Authorized** (`A`), skip authorization

### 67. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IADPBALO**
- Click **Enter Query**
- Enter **FUND** in the Pool Group Code field
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

---

## CRR Account Class Tagging Workflow (crrAccountClassTagging.js)

### 68. Read CRR Account Class Data from Excel
- Read `EXCEL/CRR.xlsx`, sheet **CRR Account Classes**
- Column mapping:
  - **Account Class** → Account Class field on screen grid
- Collect all Account Class values from Excel

### 69. Navigate to Screen IPDCRRMN
- Enter function ID **IPDCRRMN** in the top-right corner
- Submit to open the CRR Account Class Tagging screen

### 70. Query Existing Records
- Click **Enter Query**
- Click **Execute Query** (no filter — loads all existing records)
- Read the Account Classes already present in the screen grid

### 71. Identify Missing Account Classes
- Compare the Account Classes from Excel with those already in the screen grid
- Any Account Class from Excel that is NOT in the grid is considered **missing** and must be added

### 72. Unlock and Add Missing Account Classes
- If the record exists, click **Unlock** to make it editable
- For each missing Account Class:
  - Click the **+** (Add Row) button on the grid
  - Enter the **Account Class** value in the Account Class field of the new row
- Repeat until all missing Account Classes have been added

### 73. Save Record
- Click **Save**
- If any **Override** popup appears, click **Accept**

### 74. Validation
- Confirm success message or UI confirmation

### 75. Check Authorization Status
- After saving, check the **Authorization Status** field on the screen
- If status is **Unauthorized** (`U`), proceed to step 76
- If status is **Authorized** (`A`), skip authorization

### 76. Authorize Record (second user)
- Open a second browser session and log in as the checker user (`accorder_auth_username` / `accorder_auth_password`)
- Navigate to screen **IPDCRRMN**
- Click **Enter Query**
- Click **Execute Query** to load the record
- Click **Authorize**
- If any **Accept** popup appears, click **Accept**
- Confirm authorization success message

---

## Technical Requirements
- Use Playwright or Selenium
- Avoid hardcoded values
- Use explicit waits (no sleep)
- Include error handling
- Add logging
- Write modular, clean code

---


